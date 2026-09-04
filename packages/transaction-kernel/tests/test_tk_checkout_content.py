"""Canonical checkout content, ADR 0003 D6.

The single most important test in this file is the frozen regression vector. The hash it
pins is the kind of value stored inside approvals, receipts and grants; if the builder or
the canonicalizer ever produces a different string for the same document, every stored
approval becomes unverifiable at admission. That test failing is not a test to update, it
is a contract break to revert or to version under a new ``CONTENT_VERSION``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash
from transaction_kernel.checkout_content import (
    CONTENT_KEYS,
    CONTENT_VERSION,
    ContentContractError,
    ContentLine,
    build_checkout_content,
    content_hash,
    lines_of,
    total_of,
    units_of,
    validate_checkout_content,
)

CHECKOUT_ID = uuid.UUID("01900000-0000-7000-8000-000000000001")
SOURCE = "merchant-sim:demo-grocery/v1"

RICE = ContentLine(
    sku="GRO-STPL-001",
    name="Basmati rice 5 kg",
    quantity=1,
    unit_minor=49900,
    line_minor=49900,
    tax_minor=2495,
)
MILK = ContentLine(
    sku="GRO-DAIRY-001",
    name="Toned milk 1 L",
    quantity=2,
    unit_minor=2800,
    line_minor=5600,
    tax_minor=0,
)


def build(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "checkout_id": CHECKOUT_ID,
        "version": 1,
        "currency": "INR",
        "lines": (RICE, MILK),
        "subtotal_minor": 55500,
        "tax_minor": 2495,
        "delivery_fee_minor": 0,
        "discount_minor": 0,
        "total_minor": 57995,
        "policy_version": "pol-v12",
        "catalogue_revision": 0,
        "source_id": SOURCE,
    }
    fields.update(overrides)
    return build_checkout_content(**fields)


# ------------------------------------------------------------------ frozen regression vector

# FROZEN. This document and this hash are pinned on purpose. Changing either -- a key, a
# sort order, the version tag, the canonicalizer -- invalidates EVERY stored approval,
# receipt binding and Execution Grant that names a hash computed under
# ``checkout_content/1``. If this test fails, revert the change or introduce a new
# CONTENT_VERSION with a verifier that understands both; never edit the literal below.
FROZEN_VECTOR: dict[str, Any] = {
    "content_version": "checkout_content/1",
    "checkout_id": "01900000-0000-7000-8000-000000000001",
    "version": 1,
    "currency": "INR",
    "lines": [
        {
            "sku": "GRO-DAIRY-001",
            "name": "Toned milk 1 L",
            "quantity": 2,
            "unit_minor": 2800,
            "line_minor": 5600,
            "tax_minor": 0,
        },
        {
            "sku": "GRO-STPL-001",
            "name": "Basmati rice 5 kg",
            "quantity": 1,
            "unit_minor": 49900,
            "line_minor": 49900,
            "tax_minor": 2495,
        },
    ],
    "line_items": {"GRO-DAIRY-001": 2, "GRO-STPL-001": 1},
    "subtotal_minor": 55500,
    "tax_minor": 2495,
    "delivery_fee_minor": 0,
    "discount_minor": 0,
    "total_minor": 57995,
    "policy_version": "pol-v12",
    "catalogue_revision": 0,
    "source_id": "merchant-sim:demo-grocery/v1",
}
FROZEN_HASH = "UT8NPKrGlLn1VQL8sXi7MKB7ZvxFBEcjAFpw8czKKzk"


class TestFrozenRegressionVector:
    def test_the_pinned_hash_is_reproduced(self) -> None:
        assert content_hash(FROZEN_VECTOR) == FROZEN_HASH

    def test_the_builder_reproduces_the_pinned_document(self) -> None:
        assert build() == FROZEN_VECTOR

    def test_the_hash_is_commerce_domain_canonical_hash_over_the_document(self) -> None:
        # No private hashing scheme: an external verifier with only commerce_domain and the
        # stored JSONB must arrive at the same string.
        assert canonical_hash(FROZEN_VECTOR) == FROZEN_HASH

    def test_the_version_tag_is_inside_the_hash(self) -> None:
        assert FROZEN_VECTOR["content_version"] == CONTENT_VERSION
        assert "content_version" in CONTENT_KEYS


# ------------------------------------------------------------------------------- builder


class TestBuilder:
    def test_lines_are_sorted_by_sku_whatever_order_the_caller_used(self) -> None:
        assert build(lines=(MILK, RICE)) == build(lines=(RICE, MILK))

    def test_line_items_is_the_sku_to_quantity_projection(self) -> None:
        assert build()["line_items"] == {"GRO-DAIRY-001": 2, "GRO-STPL-001": 1}

    def test_both_reservation_and_delta_shapes_are_present(self) -> None:
        content = build()
        # reservations._units_wanted reads lines[].sku / lines[].quantity
        assert [(line["sku"], line["quantity"]) for line in content["lines"]] == [
            ("GRO-DAIRY-001", 2),
            ("GRO-STPL-001", 1),
        ]
        # admission's legacy shape reads line_items
        assert content["line_items"]["GRO-STPL-001"] == 1

    def test_duplicate_sku_is_refused(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(lines=(RICE, RICE))
        assert info.value.path == "lines[1].sku"

    def test_empty_basket_is_refused(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(lines=())
        assert info.value.path == "lines"

    def test_subtotal_must_equal_the_line_sum(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(subtotal_minor=55501, total_minor=57996)
        assert info.value.path == "subtotal_minor"

    def test_total_must_equal_the_components(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(total_minor=57994)
        assert info.value.path == "total_minor"

    def test_tax_cannot_be_below_the_line_taxes(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(tax_minor=2494, total_minor=57994)
        assert info.value.path == "tax_minor"

    def test_delivery_fee_and_discount_enter_the_total(self) -> None:
        content = build(delivery_fee_minor=2500, discount_minor=500, total_minor=59995)
        assert content["total_minor"] == 59995

    def test_a_bool_is_not_an_integer(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(catalogue_revision=True)
        assert info.value.path == "catalogue_revision"

    def test_negative_money_is_refused(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(discount_minor=-1, total_minor=57996)
        assert info.value.path == "discount_minor"

    def test_checkout_id_must_be_a_uuid_object(self) -> None:
        with pytest.raises(ContentContractError) as info:
            build(checkout_id=str(CHECKOUT_ID))
        assert info.value.path == "checkout_id"


class TestContentLine:
    def test_line_minor_must_equal_unit_times_quantity(self) -> None:
        with pytest.raises(ContentContractError) as info:
            ContentLine(sku="x", name="X", quantity=2, unit_minor=100, line_minor=150, tax_minor=0)
        assert info.value.path == "line.line_minor"

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ContentContractError):
            ContentLine(sku="x", name="X", quantity=0, unit_minor=100, line_minor=0, tax_minor=0)

    def test_from_content_refuses_an_extra_key(self) -> None:
        line = RICE.as_content() | {"note": "free text"}
        with pytest.raises(ContentContractError) as info:
            ContentLine.from_content(line, "lines[0]")
        assert info.value.path == "lines[0]"


# ----------------------------------------------------------------------------- validator


class TestValidator:
    def test_accepts_the_builder_output(self) -> None:
        validate_checkout_content(build())

    def test_refuses_a_non_mapping(self) -> None:
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(["not", "an", "object"])
        assert info.value.path == "$"

    def test_refuses_an_unknown_top_level_key(self) -> None:
        content = build() | {"observed_at": 1}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "$"

    def test_refuses_a_missing_key(self) -> None:
        content = build()
        del content["source_id"]
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "$"

    def test_refuses_another_content_version(self) -> None:
        content = build() | {"content_version": "checkout_content/2"}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "content_version"

    def test_refuses_a_non_canonical_uuid_spelling(self) -> None:
        # Braces are accepted by uuid.UUID but are not the canonical spelling.
        content = build() | {"checkout_id": "{" + str(CHECKOUT_ID) + "}"}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "checkout_id"

    def test_refuses_version_zero(self) -> None:
        content = build() | {"version": 0}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "version"

    def test_refuses_an_unsupported_currency(self) -> None:
        content = build() | {"currency": "inr"}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "currency"

    def test_refuses_unsorted_lines(self) -> None:
        content = build()
        content["lines"] = list(reversed(content["lines"]))
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "lines"

    def test_refuses_line_items_that_disagree_with_lines(self) -> None:
        content = build()
        content["line_items"] = {"GRO-DAIRY-001": 2, "GRO-STPL-001": 3}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "line_items"

    def test_refuses_a_float_total(self) -> None:
        content = build() | {"total_minor": 57995.0}
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "total_minor"

    def test_refuses_a_malformed_line_with_its_path(self) -> None:
        content = build()
        content["lines"][0]["quantity"] = "2"
        with pytest.raises(ContentContractError) as info:
            validate_checkout_content(content)
        assert info.value.path == "lines[0].quantity"

    def test_the_error_message_starts_with_the_path(self) -> None:
        with pytest.raises(ContentContractError, match=r"^total_minor: "):
            build(total_minor=1)


# --------------------------------------------------------------------------- projections


class TestProjections:
    def test_total_of(self) -> None:
        assert total_of(build()) == Money(57995, "INR")

    def test_units_of(self) -> None:
        assert units_of(build()) == {"GRO-DAIRY-001": 2, "GRO-STPL-001": 1}

    def test_lines_of_round_trips_the_builder_input(self) -> None:
        assert lines_of(build()) == (MILK, RICE)

    def test_projections_validate_first(self) -> None:
        with pytest.raises(ContentContractError):
            total_of(build() | {"total_minor": 1})
        with pytest.raises(ContentContractError):
            units_of(build() | {"line_items": {}})

    def test_admissions_restamp_of_n_plus_one_still_validates_and_changes_the_hash(self) -> None:
        """admission.CurrentMerchantState.content_for_hash copies the document and sets
        checkout_id and version; the result must be canonical and must hash differently,
        because version N+1 is a different approval."""
        original = build()
        restamped = dict(original)
        restamped["checkout_id"] = str(CHECKOUT_ID)
        restamped["version"] = 2
        validate_checkout_content(restamped)
        assert content_hash(restamped) != content_hash(original)

    def test_content_hash_is_deterministic_across_key_order(self) -> None:
        content = build()
        shuffled = dict(reversed(list(content.items())))
        assert content_hash(shuffled) == content_hash(content)

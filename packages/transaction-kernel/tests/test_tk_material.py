"""What the kernel calls a material change.

The comparator decides whether an approval still stands, so these tests are written from
both directions: what it must catch, and what it must leave alone. The second half matters
as much as the first. A comparator that refuses too much invalidates every open checkout in
a shop each time anything in it moves, which is a worse product than one that refuses too
little and a harder failure to notice, because a refusal always looks like the system
working.
"""

from __future__ import annotations

from typing import Any

from commerce_domain import uuid7
from transaction_kernel.checkout_content import (
    CONTENT_KEYS,
    LINE_KEYS,
    ContentLine,
    build_checkout_content,
)
from transaction_kernel.material import (
    IDENTITY_KEYS,
    IMMATERIAL_KEYS,
    MATERIAL_LINE_KEYS,
    MATERIAL_TOP_LEVEL,
    REPORTED_BY_CALLER,
    material_deltas,
)

RICE = "INDI-STPL-001"
MILK = "AMUL-DAIRY-001"
DAL = "TATA-STPL-007"

CHECKOUT = uuid7()


def document(
    *,
    version: int = 1,
    lines: tuple[ContentLine, ...] | None = None,
    subtotal: int = 55500,
    tax: int = 2495,
    delivery: int = 0,
    discount: int = 0,
    total: int = 57995,
    revision: int = 0,
    source: str = "merchant-sim:demo-grocery/v1",
    policy: str = "pol-v12",
) -> dict[str, Any]:
    """A canonical document. Defaults are the rice-and-milk cart the suite uses."""
    return build_checkout_content(
        checkout_id=CHECKOUT,
        version=version,
        currency="INR",
        lines=lines
        or (
            ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
            ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0),
        ),
        subtotal_minor=subtotal,
        tax_minor=tax,
        delivery_fee_minor=delivery,
        discount_minor=discount,
        total_minor=total,
        policy_version=policy,
        catalogue_revision=revision,
        source_id=source,
    )


class TestThePartition:
    """Every content key is deliberately on one side of the line."""

    def test_it_covers_the_content_contract_exactly(self) -> None:
        assert MATERIAL_TOP_LEVEL | IDENTITY_KEYS | IMMATERIAL_KEYS == CONTENT_KEYS

    def test_the_three_groups_do_not_overlap(self) -> None:
        assert not MATERIAL_TOP_LEVEL & IDENTITY_KEYS
        assert not MATERIAL_TOP_LEVEL & IMMATERIAL_KEYS
        assert not IDENTITY_KEYS & IMMATERIAL_KEYS

    def test_the_caller_reported_key_is_material(self) -> None:
        """It is not exempt from mattering, only from being reported twice."""
        assert REPORTED_BY_CALLER <= MATERIAL_TOP_LEVEL

    def test_line_keys_compare_the_purchase_and_not_its_label(self) -> None:
        assert MATERIAL_LINE_KEYS < LINE_KEYS
        assert "sku" not in MATERIAL_LINE_KEYS, "sku is the identity a line is matched on"
        assert "name" not in MATERIAL_LINE_KEYS, "a corrected spelling is not a price change"


class TestWhatItLeavesAlone:
    def test_two_identical_documents_produce_nothing(self) -> None:
        assert material_deltas(document(), document()) == []

    def test_an_unrelated_catalogue_edit_does_not_touch_this_purchase(self) -> None:
        """The reason this is a comparator and not a hash equality test.

        ``catalogue_revision`` is inside the hashed key set and advances on every store
        mutation, so a price change on a product this buyer never saw moves it. Comparing
        digests would refuse every open checkout in the shop.
        """
        assert material_deltas(document(revision=0), document(revision=41)) == []

    def test_a_different_quoting_source_is_not_a_change(self) -> None:
        assert material_deltas(document(), document(source="connector:live/v2")) == []

    def test_a_new_policy_version_is_not_a_material_change(self) -> None:
        """Publishing terms does not re-open a purchase.

        The terms this sale is governed by were frozen into its Policy-at-Sale Receipt.
        A later publication governs later sales.
        """
        assert material_deltas(document(), document(policy="pol-v13")) == []

    def test_the_version_number_itself_is_not_a_change(self) -> None:
        """N and N+1 differ here by construction, on every supersede."""
        assert material_deltas(document(version=1), document(version=2)) == []

    def test_a_renamed_product_is_not_a_change(self) -> None:
        renamed = document(
            lines=(
                ContentLine(RICE, "Basmati Rice, 5 kg pack", 1, 49900, 49900, 2495),
                ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0),
            )
        )
        assert material_deltas(document(), renamed) == []


class TestWhatItCatches:
    def test_a_same_total_change_names_every_component_that_moved(self) -> None:
        """The defect this work exists for.

        Milk falls, a delivery fee appears, tax moves with the mix, and the total lands on
        exactly the figure the buyer approved. The old comparison saw two equal integers
        and let the money go.
        """
        moved = document(
            lines=(
                ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
                ContentLine(MILK, "Toned milk 1 L", 2, 1325, 2650, 0),
            ),
            subtotal=52550,
            tax=2945,
            delivery=2500,
            total=57995,
        )
        deltas = material_deltas(document(), moved)
        paths = {d.field_path for d in deltas}
        assert paths == {
            "subtotal_minor",
            "tax_minor",
            "delivery_fee_minor",
            f"lines[{MILK}].unit_minor",
            f"lines[{MILK}].line_minor",
        }
        by_path = {d.field_path: d for d in deltas}
        assert by_path["delivery_fee_minor"].approved == 0
        assert by_path["delivery_fee_minor"].current == 2500
        assert by_path[f"lines[{MILK}].unit_minor"].reason == "unit_price_changed"

    def test_a_withdrawn_line_is_reported_as_a_quantity_falling_to_zero(self) -> None:
        remaining = document(
            lines=(ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),),
            subtotal=49900,
            total=52395,
        )
        deltas = material_deltas(document(), remaining)
        gone = [d for d in deltas if d.field_path == f"lines[{MILK}].quantity"]
        assert len(gone) == 1
        assert gone[0].approved == 2
        assert gone[0].current == 0
        assert gone[0].reason == "item_unavailable"

    def test_an_added_line_is_reported_too(self) -> None:
        """A merchant adding to a purchase is as material as one taking from it."""
        bigger = document(
            lines=(
                ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
                ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0),
                ContentLine(DAL, "Toor dal 1 kg", 1, 18000, 18000, 900),
            ),
            subtotal=73500,
            tax=3395,
            total=76895,
        )
        deltas = material_deltas(document(), bigger)
        added = [d for d in deltas if d.field_path == f"lines[{DAL}].quantity"]
        assert len(added) == 1
        assert added[0].reason == "item_added"

    def test_lines_are_matched_by_sku_and_not_by_position(self) -> None:
        """A withdrawn line must not report every line after it as changed."""
        three = document(
            lines=(
                ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
                ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0),
                ContentLine(DAL, "Toor dal 1 kg", 1, 18000, 18000, 900),
            ),
            subtotal=73500,
            tax=3395,
            total=76895,
        )
        without_milk = document(
            lines=(
                ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
                ContentLine(DAL, "Toor dal 1 kg", 1, 18000, 18000, 900),
            ),
            subtotal=67900,
            tax=3395,
            total=71295,
        )
        deltas = material_deltas(three, without_milk)
        line_paths = [d.field_path for d in deltas if d.field_path.startswith("lines[")]
        assert line_paths == [f"lines[{MILK}].quantity"]

    def test_a_currency_change_is_material(self) -> None:
        other = dict(document())
        other["currency"] = "USD"
        deltas = material_deltas(document(), other)
        assert [d.reason for d in deltas if d.field_path == "currency"] == ["currency_changed"]

    def test_the_discount_is_compared(self) -> None:
        """Reserved for the offer work: an offer appearing or ending moves this key."""
        discounted = document(discount=5000, total=52995)
        deltas = material_deltas(document(), discounted)
        assert [d.field_path for d in deltas] == ["discount_minor"]


class TestItRefusesToGuess:
    def test_the_total_is_left_to_the_caller(self) -> None:
        """One changed total must produce one row, not two.

        Admission compares the approval record's amount against merchant truth, which is a
        different fact from two documents disagreeing, and it reports that itself. Nothing
        is lost by leaving it out here: the content contract refuses any document whose
        components do not sum to its total, so a total that moved always has a component
        that moved with it, and that component is named.
        """
        dearer = document(delivery=2500, total=60495)
        paths = [d.field_path for d in material_deltas(document(), dearer)]
        assert paths == ["delivery_fee_minor"]
        assert "total_minor" not in paths

    def test_a_non_canonical_document_yields_nothing(self) -> None:
        """The legacy minimal shape a state source may project.

        Silence rather than invention: these rows are the evidence a buyer is shown, and a
        document whose shape is unknown cannot support a claim about what moved. The
        caller's own total comparison still refuses the payment.
        """
        legacy = {
            "checkout_id": str(CHECKOUT),
            "version": 1,
            "currency": "INR",
            "total_minor": 57995,
            "line_items": {RICE: 1, MILK: 2},
            "policy_version": "pol-v12",
        }
        other = dict(legacy, total_minor=99999)
        assert material_deltas(legacy, other) == []

    def test_a_missing_document_yields_nothing(self) -> None:
        assert material_deltas(None, document()) == []
        assert material_deltas(document(), None) == []

    def test_rows_come_back_in_a_stable_order(self) -> None:
        """A buyer re-reading a refusal should not find its rows shuffled."""
        moved = document(
            lines=(
                ContentLine(RICE, "Basmati rice 5 kg", 1, 45000, 45000, 2250),
                ContentLine(MILK, "Toned milk 1 L", 2, 1325, 2650, 0),
            ),
            subtotal=47650,
            tax=2250,
            delivery=2500,
            total=52400,
        )
        first = [d.field_path for d in material_deltas(document(), moved)]
        second = [d.field_path for d in material_deltas(document(), moved)]
        assert first == second
        # Document-level rows first, then per-line rows: a buyer reads the summary before
        # the itemisation. Each group is sorted within itself.
        top = [p for p in first if not p.startswith("lines[")]
        lines = [p for p in first if p.startswith("lines[")]
        assert first == top + lines
        assert top == sorted(top)
        assert lines == sorted(lines)

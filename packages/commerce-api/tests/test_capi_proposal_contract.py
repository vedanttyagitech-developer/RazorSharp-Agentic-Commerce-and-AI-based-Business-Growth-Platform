"""The growth proposal contract, pinned where both halves of the platform can see it.

Two producers put a proposal on the wire -- the agent runtime's ``growth_proposal_create``
tool and the deterministic runner in ``agent_service`` -- and one merchant console parses
it. The console is TypeScript and cannot import a Python dataclass, so the thing they agree
on is a *record on disk*: ``fixtures/golden/growth_proposal.json``.

That file is the hinge. This module asserts the shared builder produces it exactly, and
``apps/merchant-console/src/features/copilot/contract.test.ts`` asserts the console parses
that same file and offers to apply it. Renaming a key on either side breaks one of the two
suites, which is the entire point: this project has already paid once for a producer that
said ``deltas`` and a consumer that read ``items``, and the cost was a refusal card that
rendered empty and stayed that way.

Nothing here touches a database. The builder is pure, and a contract test that needed a
running platform to state what the contract *is* would be a test of the platform instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from agent_runtime.backends.memory import LOW_STOCK_UNITS
from agent_runtime.capabilities.proposals import (
    LEVER_TOP_SELLER_OUT_OF_STOCK,
    PROPOSAL_APPLY_ENDPOINT,
    PROPOSAL_ID_PREFIX,
    REQUIRED_EVIDENCE_KEYS,
    REQUIRED_PROPOSAL_KEYS,
    RESTOCK_FLOOR_UNITS,
    proposal_record,
    restock_draft,
    subject_record,
)
from agent_runtime.grounding.fence import fence_untrusted
from commerce_api.services.agent_service import _LOW_STOCK_UNITS as _DETERMINISTIC_LOW_STOCK_UNITS

#: The record both language suites read. Four directories up from this file is the
#: repository root, which is where a fixture shared across packages has to live: putting it
#: inside either package would make one of the two producers its owner.
GOLDEN: Final[Path] = (
    Path(__file__).resolve().parents[3] / "fixtures" / "golden" / "growth_proposal.json"
)

#: The figures the golden record rests on. They are the demo tenant's real ones -- 247
#: products, three of them listed with an empty shelf -- so that the fixture reads as
#: something the platform actually said rather than as a shape somebody invented.
GOLDEN_SKU: Final[str] = "AMUL-DAIRY-004"
GOLDEN_NAME: Final[str] = "Amul Malai Paneer Block 200 g"
GOLDEN_TOTAL: Final[int] = 247
GOLDEN_REVISION: Final[int] = 3
GOLDEN_EMPTY_ROWS: Final[int] = 3


def build_golden() -> dict[str, Any]:
    """The golden record, built the way both producers build one.

    Exported rather than inlined because the deterministic-runner test in
    ``test_capi_agent.py`` compares the shape of a live turn's proposal against this, and a
    second spelling of the same inputs would be a second contract.
    """
    # Fenced exactly as both producers fence it. ``merchant_text`` is what a model reads,
    # so it keeps its ``<merchant_data>`` wrapper; the card row label is what a person reads
    # and is the plain name. Recording the wrapper in the fixture rather than the bare name
    # is the honest choice: it is what actually goes on the wire.
    fenced = fence_untrusted(GOLDEN_NAME)
    subject = subject_record(
        sku=GOLDEN_SKU,
        merchant_text=fenced.text,
        quarantined=fenced.suspicious,
        basis="listed_out_of_stock",
    )
    draft = restock_draft(
        subject=subject,
        subject_label=GOLDEN_NAME,
        stock_units=0,
        catalogue_total=GOLDEN_TOTAL,
        catalogue_revision=GOLDEN_REVISION,
        listed_with_no_stock=GOLDEN_EMPTY_ROWS,
        rows_in_this_state=GOLDEN_EMPTY_ROWS,
    )
    return proposal_record(LEVER_TOP_SELLER_OUT_OF_STOCK, draft)


def float_paths(value: Any, path: str = "") -> list[str]:
    """Every path in the record at which a float sits. Money is integer minor units.

    Public because the deterministic runner's test in ``test_capi_agent.py`` asserts the
    same thing about a live turn, and the rule is the platform's rather than this file's.
    """
    if isinstance(value, float):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in float_paths(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in float_paths(v, f"{path}[{i}]")]
    return []


def test_the_golden_record_is_what_the_shared_builder_produces() -> None:
    """The fixture the console parses is byte-for-byte what this platform emits.

    A failure here is one of two things and both matter: the builder changed and the
    console was not told, or the fixture was edited to make a console test pass and the
    platform never agreed to it.
    """
    assert GOLDEN.exists(), f"the shared contract fixture is missing at {GOLDEN}"
    assert json.loads(GOLDEN.read_text()) == build_golden()


def test_the_record_carries_every_key_the_console_requires() -> None:
    record = build_golden()
    assert record.keys() >= REQUIRED_PROPOSAL_KEYS
    assert record["evidence"].keys() >= REQUIRED_EVIDENCE_KEYS
    assert record["kind"] == "proposal"
    assert record["proposal_id"].startswith(PROPOSAL_ID_PREFIX)
    assert record["evidence"]["read_by"], "a proposal that names no tool has no evidence"


def test_a_proposal_states_that_the_data_is_synthetic() -> None:
    """Specification 6.6 requires it, and a flag a merchant has to go looking for is not one."""
    assert build_golden()["evidence"]["synthetic"] is True


def test_a_proposal_arrives_unapplied_and_names_where_a_person_applies_it() -> None:
    record = build_golden()
    assert record["applied"] is False
    assert record["where"] == "merchant_console"


def test_the_change_is_data_naming_the_one_endpoint_a_person_posts_to() -> None:
    """The console refuses to offer apply for any other endpoint, so this string is load-bearing."""
    change = build_golden()["change"]
    assert change["endpoint"] == PROPOSAL_APPLY_ENDPOINT
    assert change["body"]["kind"] == "STOCK_SET"
    assert change["body"]["value"] == RESTOCK_FLOOR_UNITS
    assert change["reversible"] is True
    assert change["reverses_to"] == {"kind": "STOCK_SET", "sku": GOLDEN_SKU, "value": 0}


def test_a_proposal_reversible_flag_matches_whether_a_prior_state_was_observed() -> None:
    """An undo nobody wrote down is not an undo, so the flag follows the observation."""
    subject = subject_record(
        sku=GOLDEN_SKU,
        merchant_text=fence_untrusted(GOLDEN_NAME).text,
        quarantined=False,
        basis="listed_out_of_stock",
    )
    draft = restock_draft(
        subject=subject,
        subject_label=GOLDEN_NAME,
        stock_units=None,
        catalogue_total=GOLDEN_TOTAL,
        catalogue_revision=GOLDEN_REVISION,
        listed_with_no_stock=GOLDEN_EMPTY_ROWS,
        rows_in_this_state=GOLDEN_EMPTY_ROWS,
    )
    change = proposal_record(LEVER_TOP_SELLER_OUT_OF_STOCK, draft)["change"]
    assert change["reversible"] is False
    assert change["reverses_to"] is None


def test_no_float_appears_anywhere_in_a_proposal() -> None:
    assert float_paths(build_golden(), "proposal") == []


def test_the_id_is_stable_for_identical_figures_and_moves_when_one_changes() -> None:
    """An id that still matches is a record whose evidence has not moved underneath it."""
    assert build_golden()["proposal_id"] == build_golden()["proposal_id"]
    moved = restock_draft(
        subject=subject_record(
            sku=GOLDEN_SKU,
            merchant_text=fence_untrusted(GOLDEN_NAME).text,
            quarantined=False,
            basis="listed_out_of_stock",
        ),
        subject_label=GOLDEN_NAME,
        stock_units=0,
        catalogue_total=GOLDEN_TOTAL,
        catalogue_revision=GOLDEN_REVISION + 1,
        listed_with_no_stock=GOLDEN_EMPTY_ROWS,
        rows_in_this_state=GOLDEN_EMPTY_ROWS,
    )
    assert (
        proposal_record(LEVER_TOP_SELLER_OUT_OF_STOCK, moved)["proposal_id"]
        != (build_golden()["proposal_id"])
    )


def test_renaming_a_product_does_not_change_what_its_shelf_says() -> None:
    """The merchant's own text is not an input to the id, deliberately."""
    renamed = restock_draft(
        subject=subject_record(
            sku=GOLDEN_SKU,
            merchant_text=fence_untrusted("Paneer, but the merchant retitled it").text,
            quarantined=False,
            basis="listed_out_of_stock",
        ),
        subject_label="Paneer, but the merchant retitled it",
        stock_units=0,
        catalogue_total=GOLDEN_TOTAL,
        catalogue_revision=GOLDEN_REVISION,
        listed_with_no_stock=GOLDEN_EMPTY_ROWS,
        rows_in_this_state=GOLDEN_EMPTY_ROWS,
    )
    assert (
        proposal_record(LEVER_TOP_SELLER_OUT_OF_STOCK, renamed)["proposal_id"]
        == (build_golden()["proposal_id"])
    )


def test_the_title_and_rationale_never_carry_the_merchant_s_own_text() -> None:
    """A product name is text the merchant wrote; a title is the platform speaking."""
    record = build_golden()
    assert GOLDEN_NAME not in record["title"]
    assert GOLDEN_NAME not in record["rationale"]
    # Fenced for the model rather than bare, which is why the assertion is containment:
    # what matters is that the name reached the record only in the field meant for it.
    assert record["evidence"]["subject"]["merchant_text"] == fence_untrusted(GOLDEN_NAME).text


def test_nothing_in_the_record_claims_a_comparison_with_another_merchant() -> None:
    """There is one tenant's data in scope, so a benchmark would be an invented fact."""
    prose = json.dumps(build_golden()).lower()
    for forbidden in ("benchmark", "industry average", "compared to other", "peer"):
        assert forbidden not in prose


def test_the_restock_floor_is_the_level_at_which_the_diagnostic_goes_quiet() -> None:
    """The rationale's one quantitative promise, held against the diagnostic that answers.

    The record tells a merchant that the proposed level is "the lowest at which this
    platform's inventory diagnostic stops reporting the product". Two diagnostics compute
    that on this platform -- the agent runtime's and the deterministic runner's -- and for a
    while they disagreed: 5 units and a strict ``<`` on one side, 3 and ``<=`` on the other.
    A product restocked to the proposed 4 therefore went quiet on one half and stayed
    flagged as low stock on the other, which was discovered by applying a real proposal and
    re-reading the catalogue.

    Pinning the rule rather than the number is deliberate: the threshold may move, but the
    floor must stay one unit past whatever it is, or the sentence stops being true.
    """
    assert RESTOCK_FLOOR_UNITS == LOW_STOCK_UNITS + 1

    # Both halves classify by the same rule, so a shelf at the floor is reported by neither.
    assert not 0 < RESTOCK_FLOOR_UNITS <= LOW_STOCK_UNITS
    assert not 0 < RESTOCK_FLOOR_UNITS <= _DETERMINISTIC_LOW_STOCK_UNITS
    assert _DETERMINISTIC_LOW_STOCK_UNITS == LOW_STOCK_UNITS, (
        "one platform, one definition of low stock"
    )

    # And the level just below the floor is still reported, which is what makes it a floor
    # rather than an arbitrary number that happens to be quiet.
    assert 0 < RESTOCK_FLOOR_UNITS - 1 <= LOW_STOCK_UNITS

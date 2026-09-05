"""Grounded multilingual search.

The load-bearing claims: Hindi, Hinglish and English reach the same products; ordering is
identical on every run; results carry provenance; and an out-of-stock item is reported
honestly rather than hidden.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from commerce_domain import Money
from merchant_sim.grounding import SOURCE_ID
from merchant_sim.scenarios import ScenarioController
from merchant_sim.search import Locale, search
from merchant_sim.store import MerchantStore

MILK_SKUS = {"AMUL-DAIRY-001", "AMUL-DAIRY-002", "NEST-DAIRY-006"}


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore(clock=frozen_clock)


def skus(store: MerchantStore, query: str, limit: int = 10) -> tuple[str, ...]:
    return search(query, store=store, limit=limit).skus()


class TestMultilingualMatching:
    @pytest.mark.parametrize("query", ["doodh", "dodh", "dudh", "duudh", "दूध", "milk", "Doodh"])
    def test_every_spelling_of_milk_finds_milk(self, store: MerchantStore, query: str) -> None:
        found = set(skus(store, query))
        assert found >= MILK_SKUS, f"{query!r} missed {MILK_SKUS - found}"

    def test_hinglish_and_devanagari_agree_across_the_catalogue(self, store: MerchantStore) -> None:
        # If these diverge, a buyer who switches keyboards mid-sentence gets a different
        # store, which is exactly the failure the shared index exists to prevent.
        pairs = [
            ("cheeni", "चीनी"),
            ("aloo", "आलू"),
            ("pyaz", "प्याज"),
            ("dahi", "दही"),
            ("namak", "नमक"),
            ("achar", "अचार"),
            ("chawal", "चावल"),
            ("haldi", "हल्दी"),
        ]
        for latin, devanagari in pairs:
            latin_top = skus(store, latin)[0]
            devanagari_top = skus(store, devanagari)[0]
            assert latin_top == devanagari_top, (
                f"{latin} -> {latin_top} but {devanagari} -> {devanagari_top}"
            )

    def test_locale_changes_the_display_name_and_nothing_else(self, store: MerchantStore) -> None:
        english = search("doodh", Locale.EN, store=store)
        hindi = search("doodh", Locale.HI, store=store)
        hinglish = search("doodh", Locale.HI_LATN, store=store)
        assert english.skus() == hindi.skus() == hinglish.skus()
        assert hindi.hits[0].display_name == hindi.hits[0].view.product.name_hi
        # Hinglish is Hindi in Latin script, so it reads the Latin-script name.
        assert hinglish.hits[0].display_name == english.hits[0].view.product.name_en

    def test_indian_staples_resolve_transliterations_and_scripts(
        self, store: MerchantStore
    ) -> None:
        # Verify doodh, दूध, milk, atta, aata, आटा, and chawal resolve to the right products
        assert "AASH-STPL-002" in skus(store, "atta")
        assert "AASH-STPL-002" in skus(store, "aata")
        assert "AASH-STPL-002" in skus(store, "आटा")
        assert "INDI-STPL-001" in skus(store, "chawal")
        assert "INDI-STPL-001" in skus(store, "चावल")
        assert MILK_SKUS & set(skus(store, "doodh"))
        assert MILK_SKUS & set(skus(store, "दूध"))
        assert MILK_SKUS & set(skus(store, "milk"))
        assert "FREE-STPL-011" in skus(store, "sunflower oil") or "FREE-STPL-017" in skus(
            store, "sunflower oil"
        )
        assert "APPL-ELEC-001" in skus(store, "iphone")

    def test_typos_are_tolerated_but_different_groceries_are_not_merged(
        self, store: MerchantStore
    ) -> None:
        assert "INDI-STPL-001" in skus(store, "chawall")  # chawal typo tolerance
        assert "TOOR-STPL-004" in skus(store, "daal")  # toor dal
        # "dal" must not drag in dahi: they fold two edits apart, not one.
        assert "AMUL-DAIRY-003" not in skus(store, "dal")

    def test_awkward_names_are_reachable(self, store: MerchantStore) -> None:
        assert skus(store, "50-50")[0] == "BRIT-SNCK-002"
        assert skus(store, "haldiram")[0] == "HALD-SNCK-003"
        assert skus(store, "nescafe")[0] == "NESC-BEVG-002"
        assert skus(store, "lays")[0] == "LAYS-SNCK-004"
        # The row whose name carries a no-break space and a soft hyphen.
        assert "NIRM-HHLD-004" in skus(store, "nirma")

    def test_a_spoken_sentence_still_finds_the_product(self, store: MerchantStore) -> None:
        assert MILK_SKUS & set(skus(store, "mujhe doodh chahiye"))
        assert "MAGG-SNCK-001" in skus(store, "kuch maggi bhi add karo")

    def test_multi_word_query_ranks_the_specific_product_first(self, store: MerchantStore) -> None:
        # "aloo bhujia" must be the snack, not the potato that shares one token with it.
        result = skus(store, "aloo bhujia")
        assert result[0] == "HALD-SNCK-003"
        assert "POTA-PROD-003" in result


class TestGroundingAndDeterminism:
    def test_results_carry_source_and_revision(self, store: MerchantStore) -> None:
        result = search("doodh", store=store)
        assert result.source == SOURCE_ID
        assert result.catalogue_revision == store.revision
        assert not store.is_stale(result.freshness)

    def test_hits_carry_live_price_and_stock(self, store: MerchantStore) -> None:
        hit = search("doodh", store=store).hits[0]
        assert hit.view.unit_price == store.get_product(hit.sku).unit_price
        assert hit.availability.available_units == store.check_inventory(hit.sku).available_units

    def test_ordering_is_identical_across_repeated_calls(self, store: MerchantStore) -> None:
        # A search result is evidence in the proof chain; it must replay byte for byte.
        for query in ["doodh", "dal", "oil", "biscuit", "मसाला"]:
            runs = [search(query, store=store).skus() for _ in range(5)]
            assert len(set(runs)) == 1, query

    def test_ordering_is_identical_across_separately_constructed_stores(self) -> None:
        left = search("oil", store=MerchantStore(clock=frozen_clock)).skus()
        right = search("oil", store=MerchantStore(clock=frozen_clock)).skus()
        assert left == right

    def test_matched_terms_explain_the_hit(self, store: MerchantStore) -> None:
        # Grounding evidence: the agent can show why a product was proposed.
        hit = search("doodh", store=store).hits[0]
        assert hit.matched_terms
        assert all(isinstance(term, str) and term for term in hit.matched_terms)

    def test_exact_matches_outrank_folded_ones(self, store: MerchantStore) -> None:
        exact = search("doodh", store=store).hits[0].score
        folded_only = search("dodh", store=store).hits[0].score
        assert exact > folded_only

    @pytest.mark.parametrize(
        ("query", "exact_sku", "folded_sku"),
        [
            ("daal chahiye", "TOOR-STPL-004", "CHAN-STPL-005"),
            ("saabun chahiye", "DETT-PCAR-002", "VIM-HHLD-002"),
        ],
    )
    def test_an_exact_term_beats_a_fold_collision_on_the_same_token(
        self, store: MerchantStore, query: str, exact_sku: str, folded_sku: str
    ) -> None:
        # This is the claim that makes the lossy fold safe to ship: a collision widens the
        # result set but never displaces the right answer. The trailing stopword is load
        # bearing -- it is dropped from the tokens but stays in the normalized query, so
        # the phrase bonus cannot fire and the two products are separated by the scoring
        # tier alone. Without it, both tests pass even when exact and folded score the
        # same, because the exact match collects the phrase bonus as well.
        result = search(query, store=store)
        assert result.query_tokens == (query.split()[0],)
        scores = {hit.sku: hit.score for hit in result.hits}
        assert scores[exact_sku] > scores[folded_sku], scores
        assert result.skus().index(exact_sku) < result.skus().index(folded_sku)

    def test_a_stale_result_is_detectable_after_an_injection(self, store: MerchantStore) -> None:
        result = search("doodh", store=store)
        ScenarioController(store).set_price("AMUL-DAIRY-001", Money(3100, "INR"))
        assert store.is_stale(result.freshness)
        assert result.freshness.is_stale_against(store.revision)


class TestRefusalsAndEdges:
    def test_empty_and_stopword_queries_return_nothing(self, store: MerchantStore) -> None:
        # "add some of the" must not resolve to the entire catalogue.
        for query in ["", "   ", "please add some of the", "​​­"]:
            result = search(query, store=store)
            assert result.hits == (), query

    def test_a_query_matching_nothing_returns_nothing(self, store: MerchantStore) -> None:
        assert search("helicopter", store=store).hits == ()

    def test_limit_is_respected_and_must_be_positive(self, store: MerchantStore) -> None:
        assert len(search("oil", store=store, limit=1).hits) == 1
        with pytest.raises(ValueError, match="positive"):
            search("oil", store=store, limit=0)

    def test_out_of_stock_items_are_returned_not_hidden(self, store: MerchantStore) -> None:
        # The assistant must be able to say "we stock that but it is out" and offer a
        # substitute. It cannot do that if the merchant pretends the SKU is unknown.
        ScenarioController(store).sell_out("AMUL-DAIRY-001")
        result = search("doodh", store=store)
        assert "AMUL-DAIRY-001" in result.skus()
        out_of_stock = next(hit for hit in result.hits if hit.sku == "AMUL-DAIRY-001")
        assert not out_of_stock.availability.is_available
        assert out_of_stock.availability.available_units == 0

    def test_out_of_stock_falls_below_an_equally_relevant_in_stock_item(
        self, store: MerchantStore
    ) -> None:
        before = search("doodh", store=store).hits
        top_sku, top_score = before[0].sku, before[0].score
        peers = [hit.sku for hit in before if hit.score == top_score and hit.sku != top_sku]
        assert peers, "this test needs a scoring tie to be meaningful"

        ScenarioController(store).sell_out(top_sku)
        after = search("doodh", store=store).skus()
        # A stocked alternative of identical relevance is the more useful proposal.
        assert after.index(top_sku) > max(after.index(peer) for peer in peers)

    def test_stock_never_outranks_relevance(self, store: MerchantStore) -> None:
        # "chai" matches several teas exactly and atta only fuzzily. Selling out every
        # tea must not promote a barely-related in-stock product above them: availability
        # is a tiebreak, not a relevance signal.
        #
        # Which tea sorts first is deliberately not asserted. Four of them tie on score
        # and the documented tiebreak is the SKU, so pinning one here would make this
        # test fail the next time a product is renamed -- which is exactly what it did.
        before = search("chai", store=store).hits
        top_score = before[0].score
        top_skus = [hit.sku for hit in before if hit.score == top_score]
        weaker = [hit.sku for hit in before if hit.score < top_score]
        assert weaker, "this test needs a lower-scoring hit to be meaningful"

        for sku in top_skus:
            ScenarioController(store).sell_out(sku)
        after = search("chai", store=store).skus()
        # Every exact match still outranks every fuzzy one, sold out or not.
        assert max(after.index(sku) for sku in top_skus) < min(
            after.index(sku) for sku in weaker
        ), "an out-of-stock exact match fell below a fuzzy in-stock one"

    def test_expanded_indian_grocery_queries(self, store: MerchantStore) -> None:
        # Brief 7 Priority 2 verification: ordinary Indian household groceries resolve
        cases = [
            ("chini", "MADH-STPL-006"),
            ("sugar", "MADH-STPL-006"),
            ("चीनी", "MADH-STPL-006"),
            ("chawal", "INDI-STPL-001"),
            ("चावल", "INDI-STPL-001"),
            ("aata", "AASH-STPL-002"),
            ("आटा", "AASH-STPL-002"),
            ("haldi", "EVER-COND-004"),
            ("हल्दी", "EVER-COND-004"),
            ("paneer", "AMUL-DAIRY-004"),
            ("पनीर", "AMUL-DAIRY-004"),
            ("kela", "BANA-PROD-006"),
            ("केला", "BANA-PROD-006"),
            ("sabun", "DETT-PCAR-002"),
            ("साबुन", "DETT-PCAR-002"),
        ]
        for query, expected_sku in cases:
            skus = search(query, store=store).skus()
            assert expected_sku in skus, f"Query {query!r} failed to find {expected_sku}"

    def test_search_never_reveals_a_sku_outside_the_catalogue(self, store: MerchantStore) -> None:
        catalogue = set(store.all_skus())
        for query in ["doodh", "oil", "maggi", "मसाला", "50-50", "xyzzy"]:
            assert set(search(query, store=store).skus()) <= catalogue

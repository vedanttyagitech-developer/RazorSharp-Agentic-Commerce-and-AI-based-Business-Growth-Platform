"""Grounded multilingual catalogue search.

Specification 6.2: search results carry source, freshness and serviceability, and a model
may reference only the catalogue IDs the merchant actually returned. This module is what
returns them.

MATCHING IS ALWAYS MULTILINGUAL; LOCALE ONLY CHOOSES THE DISPLAY NAME
--------------------------------------------------------------------
A Hindi-speaking buyer types ``doodh`` in one breath and ``दूध`` in the next, and a voice
transcript arrives romanized regardless of what was spoken. Filtering the index by locale
would make the same buyer's two spellings behave differently for no reason a buyer could
predict. So every query is matched against every index term in both scripts, and ``locale``
decides one thing only: which name is shown back.

TIERED SCORING, HIGHEST TIER WINS PER TOKEN
-------------------------------------------
Each query token scores against a product at the best tier it reaches:

===== ====================================================== =====
Score Tier                                                   Why
===== ====================================================== =====
100   exact normalized term (name token, synonym, SKU)       the buyer said the word
70    folded term (``doodh`` == ``dodh`` == ``दूध``'s twin)   romanization variance
40    folded prefix, 3+ characters                           autocomplete and truncation
25    folded, within one edit                                a typo or a slip of the tongue
===== ====================================================== =====

Scores sum across query tokens, plus 50 when the whole normalized query occurs inside an
index phrase of a product that already matched on tokens. Keeping exact above folded is
what makes the lossy fold safe: a fold collision adds a weaker hit, it never displaces the
right answer.

DETERMINISM
-----------
Ordering is ``(-score, out-of-stock last, sku)``. The SKU tiebreak means the same query
returns the same list in the same order on every machine, every run -- necessary because a
search result is evidence in the proof chain, not a convenience.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .catalogue import CATALOGUE, Product
from .grounding import Freshness
from .store import InventoryStatus, MerchantStore, ProductView
from .textfold import fold_hinglish, normalize, tokenize, within_edit_distance_one

__all__ = ["Locale", "SearchHit", "SearchResults", "index_terms_for", "search"]

_SCORE_EXACT: Final[int] = 100
_SCORE_FOLDED: Final[int] = 70
_SCORE_PREFIX: Final[int] = 40
_SCORE_FUZZY: Final[int] = 25
_SCORE_PHRASE_BONUS: Final[int] = 50

# Below three characters a folded key is mostly vowels and matches everything; below four,
# a single edit turns one real word into another ("dal" -> "dahi" is not a typo, it is a
# different aisle). These floors are what keep the lossy tiers from producing nonsense.
_MIN_FOLD_LEN: Final[int] = 3
_MIN_FUZZY_LEN: Final[int] = 4
_MIN_PHRASE_LEN: Final[int] = 3

_DEFAULT_LIMIT: Final[int] = 10


class Locale(StrEnum):
    """Buyer locale. Chooses the display name and nothing else."""

    EN = "en-IN"
    HI = "hi-IN"
    HI_LATN = "hi-Latn-IN"

    @property
    def uses_devanagari(self) -> bool:
        """Hinglish (``hi-Latn``) is Hindi written in Latin script, so it reads the
        English-script name; only ``hi-IN`` renders Devanagari."""
        return self is Locale.HI


@dataclass(frozen=True, slots=True)
class _Index:
    """Precomputed match keys for one product."""

    sku: str
    exact_terms: frozenset[str]
    folded_terms: frozenset[str]
    phrases: frozenset[str]


def _terms_for(product: Product) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    phrases: set[str] = set()
    exact: set[str] = set()

    sources = (
        product.name_en,
        product.name_hi,
        product.unit_label,
        product.category.value,
        *product.synonyms_hi,
        *product.synonyms_latin,
    )
    for source in sources:
        phrase = normalize(source)
        if phrase:
            phrases.add(phrase)
        # Index the tokens of every source string too, so "aloo bhujia" is reachable by
        # "bhujia" alone and a multi-word synonym is not an all-or-nothing key.
        exact.update(tokenize(source))

    # The SKU is an exact term so an agent can resolve an ID it was handed without a
    # second code path, and so a typo'd ID fails as "no results" rather than as a lookup.
    exact.add(normalize(product.sku))
    exact.update(tokenize(product.sku))

    folded = {fold_hinglish(term) for term in exact}
    return frozenset(exact), frozenset(folded), frozenset(phrases)


def _build_indices() -> tuple[_Index, ...]:
    built: list[_Index] = []
    for product in CATALOGUE:
        exact, folded, phrases = _terms_for(product)
        built.append(
            _Index(sku=product.sku, exact_terms=exact, folded_terms=folded, phrases=phrases)
        )
    return tuple(built)


#: Built once at import. The catalogue fixture is immutable, so the index is too -- only
#: price, stock and availability move, and those are read live from the store per query.
_INDICES: Final[tuple[_Index, ...]] = _build_indices()
_INDEX_BY_SKU: Final[dict[str, _Index]] = {index.sku: index for index in _INDICES}


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One grounded result: the live product, why it matched, and whether it is sellable."""

    view: ProductView
    display_name: str
    score: int
    matched_terms: tuple[str, ...]
    availability: InventoryStatus

    @property
    def sku(self) -> str:
        return self.view.sku


@dataclass(frozen=True, slots=True)
class SearchResults:
    """Results plus the provenance a grounded answer is required to carry."""

    query: str
    normalized_query: str
    query_tokens: tuple[str, ...]
    locale: Locale
    hits: tuple[SearchHit, ...]
    freshness: Freshness

    @property
    def source(self) -> str:
        """Where these results came from. Stamped, never inferred by the caller."""
        return self.freshness.source

    @property
    def catalogue_revision(self) -> int:
        return self.freshness.catalogue_revision

    def skus(self) -> tuple[str, ...]:
        """The only product IDs a model may reference after this call (spec 20.1)."""
        return tuple(hit.sku for hit in self.hits)


def _score_token(token: str, index: _Index) -> tuple[int, str] | None:
    """Best tier this query token reaches against one product, and the term that did it."""
    if token in index.exact_terms:
        return _SCORE_EXACT, token

    folded = fold_hinglish(token)
    if len(folded) < _MIN_FOLD_LEN:
        # Too short to fold safely; the exact check above was its only chance.
        return None

    if folded in index.folded_terms:
        return _SCORE_FOLDED, folded

    # Deterministic scan in sorted order so the recorded matched term is reproducible when
    # several index terms tie at the same tier.
    candidates = sorted(index.folded_terms)
    for term in candidates:
        if len(term) > len(folded) and term.startswith(folded):
            return _SCORE_PREFIX, term
    if len(folded) >= _MIN_FUZZY_LEN:
        for term in candidates:
            if len(term) >= _MIN_FUZZY_LEN and within_edit_distance_one(folded, term):
                return _SCORE_FUZZY, term
    return None


def search(
    query: str,
    locale: Locale = Locale.EN,
    *,
    store: MerchantStore,
    limit: int = _DEFAULT_LIMIT,
) -> SearchResults:
    """Find catalogue products matching ``query``, grounded and stamped.

    Guarantees:

    * Hindi (``दूध``), Hinglish (``doodh``, ``dodh``, ``dudh``) and English (``milk``) all
      reach the same products, regardless of ``locale``.
    * Results are ordered deterministically and identically on every run.
    * Every hit carries live price, live stock, an availability verdict and a
      :class:`~merchant_sim.grounding.Freshness` naming the source and catalogue revision.
    * Out-of-stock matches are returned, ranked below equally relevant in-stock ones. They
      are not hidden: the assistant must be able to say "we stock that but it is out" and
      offer a substitute, which it cannot do if the merchant pretends the item is unknown.

    Refuses: a non-positive ``limit``. An empty or all-stopword query returns zero hits
    rather than the whole catalogue -- "add something to my cart" must not resolve to
    forty products.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    normalized = normalize(query)
    tokens = tokenize(query)
    freshness = store.freshness()

    if not tokens:
        return SearchResults(
            query=query,
            normalized_query=normalized,
            query_tokens=(),
            locale=locale,
            hits=(),
            freshness=freshness,
        )

    scored: list[tuple[int, int, str, tuple[str, ...]]] = []
    for index in _INDICES:
        total = 0
        matched: list[str] = []
        for token in tokens:
            hit = _score_token(token, index)
            if hit is None:
                continue
            score, term = hit
            total += score
            matched.append(term)
        if total == 0:
            continue
        # Phrase bonus, applied only to products that already matched on tokens. Gating it
        # that way is what keeps a substring test from surfacing junk: "ana" occurs inside
        # "banana" and "chana", but neither can reach the result set on that alone.
        # It is what makes "50-50" find the biscuit rather than every 50 g pack.
        if len(normalized) >= _MIN_PHRASE_LEN and any(
            normalized in phrase for phrase in index.phrases
        ):
            total += _SCORE_PHRASE_BONUS
            matched.append(normalized)
        availability = store.check_inventory(index.sku)
        # Rank key: score first, then in-stock ahead of out-of-stock, then SKU. The stock
        # term only breaks ties, so an out-of-stock exact match still beats an in-stock
        # fuzzy one -- relevance is not sacrificed to availability.
        scored.append((-total, 0 if availability.is_available else 1, index.sku, tuple(matched)))

    scored.sort()

    hits: list[SearchHit] = []
    for negative_score, _stock_rank, sku, matched_terms in scored[:limit]:
        view = store.get_product(sku)
        hits.append(
            SearchHit(
                view=view,
                display_name=view.display_name(devanagari=locale.uses_devanagari),
                score=-negative_score,
                matched_terms=matched_terms,
                availability=store.check_inventory(sku),
            )
        )

    return SearchResults(
        query=query,
        normalized_query=normalized,
        query_tokens=tokens,
        locale=locale,
        hits=tuple(hits),
        freshness=freshness,
    )


def index_terms_for(sku: str) -> frozenset[str]:
    """Exact index terms for a SKU. Exposed for catalogue-health diagnostics and tests."""
    return _INDEX_BY_SKU[sku].exact_terms

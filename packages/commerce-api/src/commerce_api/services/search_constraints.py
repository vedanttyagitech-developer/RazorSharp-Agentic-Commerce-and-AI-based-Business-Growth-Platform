"""Conservative product/price constraints; unsupported requests retain model reasoning."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .discovery import discovery_query


@dataclass(frozen=True)
class PriceSearch:
    query: str
    maximum_minor: int
    inclusive: bool


def price_search(message: str) -> PriceSearch | None:
    from agent_runtime.shopping_intent import normalize

    value = normalize(message)
    value = re.sub(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])", lambda m: m[0].replace(",", ""), value)
    match = re.fullmatch(
        r"(.+?)\s+(under|below|up to|at most)\s+₹?\s*(\d+(?:\.\d{1,2})?)\s*(?:rupees|rs\.?|inr)?",
        value,
    )
    if not match:
        hindi = re.fullmatch(
            r"(.+?)\s+₹?(\d+(?:\.\d{1,2})?)\s*(?:rupaye|rupees|रुपये|रुपए)?\s*"
            r"(ke andar|se kam|तक|के अंदर|से कम)(?:\s+(?:dikhao|दिखाओ))?",
            value,
        )
        if not hindi:
            return None
        query = discovery_query(hindi[1])
        if query is None:
            return None
        return PriceSearch(query, int(Decimal(hindi[2]) * 100), hindi[3] not in {"se kam", "से कम"})
    query = discovery_query(match[1])
    if query is None:
        return None
    return PriceSearch(query, int(Decimal(match[3]) * 100), match[2] in {"up to", "at most"})


def _terms(text: str) -> set[str]:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"(\d)\s*(ml|kg|g|l)\b", r"\1 \2", value)
    return set(re.findall(r"\w+", value))


def eligible_hits(request: PriceSearch, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required = _terms(request.query)
    selected = []
    for hit in hits:
        names = " ".join(str(hit.get(k, "")) for k in ("display_name", "name_en", "name_hi"))
        price = hit.get("unit_price_minor")
        if not isinstance(price, int) or not required.issubset(_terms(names)):
            continue
        if price > request.maximum_minor or (
            price == request.maximum_minor and not request.inclusive
        ):
            continue
        selected.append(hit)
    # Preserve relevance order within available and unavailable groups.
    return sorted(selected, key=lambda h: not h.get("is_available", False))

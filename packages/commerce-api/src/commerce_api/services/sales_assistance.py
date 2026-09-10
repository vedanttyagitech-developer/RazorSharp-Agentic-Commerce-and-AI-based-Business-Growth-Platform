"""Cart-scoped sales conversation. Read-only suggestions, never purchase authority.

Only the cart writer creates add acknowledgements. Preferences survive process restarts
for this cart; they are not a cross-shop profile. All monetary claims use fresh tools.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..deps import RequestContext
    from .agent_service import ToolExecutor, TurnOutcome

from . import cart_service
from .adaptive_shopping import stated_budget
from .search_constraints import _terms


def intent(message: str) -> str | None:
    text = message.casefold().strip(" .!?।")
    phrases = {
        "reject": (
            r"no(?: thanks)?|not interested|don't suggest that again|"
            r"no more suggestions|nahi(?: chahiye)?|nahi thanks|"
            r"mat suggest karo|नहीं(?: चाहिए)?|मत सुझाओ"
        ),
        "done": (
            r"that's all|that is all|nothing else|done shopping|"
            r"bas(?: ho gaya)?|aur kuch nahi|बस(?: हो गया)?|और कुछ नहीं"
        ),
        "confused": (
            r"i(?: am|'m) confused|help me choose|which one should i choose|"
            r"samajh nahi aa raha|kaunsa lu|कौन सा लूँ|समझ नहीं आ रहा"
        ),
        "price": r"price|lower price|cheaper please|price important hai|sasta chahiye|कम कीमत",
        "pack": r"larger pack|bigger pack|bada pack|बड़ा पैक",
        "alternative": (
            r"(?:it's |it is )?(?:unavailable|out of stock)|"
            r"alternative please|available alternative|stock nahi hai|"
            r"दूसरा उपलब्ध विकल्प"
        ),
        "budget": (
            r"(?:too expensive|over budget|budget exceed ho gaya|"
            r"बजट से ज्यादा)(?:[ ,]+(?:under|within|up to|"
            r"below)\s*₹?\s*\d+(?:\.\d{1,2})?)?"
        ),
    }
    return next((name for name, pattern in phrases.items() if re.fullmatch(pattern, text)), None)


def _local(lang: str, en: str, hi: str, hinglish: str) -> str:
    return {"hi": hi, "hi-Latn": hinglish}.get(lang, en)


def _out(
    reply: str, reason: str, hits: list[dict[str, Any]] | None = None, **extra: Any
) -> TurnOutcome:
    from .agent_service import TurnOutcome

    return TurnOutcome(
        reply=reply,
        structured={"kind": "products", "hits": hits or [], "sales_status": reason, **extra},
    )


def _finish(lang: str) -> str:
    return _local(
        lang,
        "Anything else, or shall we review your bill?",
        "और कुछ चाहिए, या बिल देखें?",
        "Aur kuch chahiye, ya bill review karein?",
    )


def _matching(
    need: dict[str, Any], tools: ToolExecutor, rejected: list[str]
) -> list[dict[str, Any]]:
    result = tools.call("catalog.search", query=need["query"], limit=50)
    if not result.ok:
        return []
    required = set().union(*(_terms(t) for t in need.get("required_terms", [])))
    # A query is also a constraint: never silently loosen a brand or connector type.
    required |= _terms(need["query"])
    excluded = [_terms(t) for t in need.get("excluded_terms", []) if t.strip()]
    return [
        h
        for h in result.payload["hits"]
        if h["sku"] not in rejected
        and h.get("is_available")
        and h.get("stock_units", 0) >= need.get("quantity", 1)
        and required.issubset(_terms(h["name_en"] + " " + h["name_hi"]))
        and not any(t.issubset(_terms(h["name_en"] + " " + h["name_hi"])) for t in excluded)
    ]


def respond(
    session: Session,
    ctx: RequestContext,
    tools: ToolExecutor,
    cart_id: uuid.UUID | None,
    message: str,
    language: str,
    event_id: uuid.UUID | None = None,
) -> TurnOutcome | None:
    kind = intent(message)
    if not kind and not event_id:
        return None
    cart = cart_service.load_cart(session, ctx, cart_id, lock=True) if cart_id else None
    if cart is not None:
        session.refresh(cart, attribute_names=["shopping_context", "lines", "status"])
    state = dict(cart.shopping_context or {}) if cart else {}

    def save() -> None:
        if cart is not None:
            cart.shopping_context = dict(state)
            session.flush()

    if event_id:
        event = state.pop("pending_add", None)
        if cart is None or not event or event["id"] != str(event_id):
            # A stale request must not consume a newer pending event.
            return _out("", "event_ignored")
        save()
        current = {line["sku"]: line["quantity"] for line in cart.lines}
        if cart.status != "OPEN" or current.get(event["sku"]) != event["quantity"]:
            return _out("", "event_ignored")
        product = tools.call("catalog.get_product", sku=event["sku"])
        if not product.ok:
            return _out(_finish(language), "ready_to_review")
        name = product.payload["display_name"]
        added = _local(
            language,
            f"Added {event['delta']} × {name}. {event['quantity']} now in your cart.",
            f"{name} के {event['delta']} पैक जोड़े। कार्ट में अब {event['quantity']} हैं।",
            f"{name} ke {event['delta']} packs add hue. Cart mein ab {event['quantity']} hain.",
        )
        # Curated food pairings only; no inferred device compatibility or health claim.
        words = _terms(product.payload["name_en"])
        pairings = {
            "milk": "bread",
            "bread": "milk",
            "coffee": "milk",
            "tea": "biscuits",
            "pasta": "pasta sauce",
            "shampoo": "conditioner",
        }
        query = next((query for word, query in pairings.items() if word in words), None)
        pitched = state.get("pitched", [])
        if (
            query
            and query not in pitched
            and not state.get("stop_suggestions")
            and not state.get("unverified_requirements")
        ):
            candidates = _matching({"query": query}, tools, state.get("rejected", []))
            candidates = [h for h in candidates if h["sku"] not in current]
            if candidates:
                hit = candidates[0]
                state["pitched"] = (pitched + [query])[-20:]
                state["last_suggestion"] = [hit["sku"]]
                state["needs"] = [{"query": query, "quantity": 1, "skus": [hit["sku"]]}]
                save()
                offer = _local(
                    language,
                    f"Would you like to consider {hit['display_name']} "
                    f"alongside it? Optional—nothing else was added.",
                    f"साथ में {hit['display_name']} देखना चाहेंगे? यह सिर्फ सुझाव है; और कुछ नहीं जोड़ा।",
                    f"Saath mein {hit['display_name']} dekhna chahenge? "
                    f"Sirf suggestion hai; aur kuch add nahi hua.",
                )
                return _out(added + " " + offer, "optional_complement", [hit])
        return _out(added + " " + _finish(language), "ready_to_review")
    if kind == "reject":
        state["rejected"] = list(
            dict.fromkeys(state.get("rejected", []) + state.get("last_suggestion", []))
        )[-50:]
        state["last_suggestion"] = []
        state["stop_suggestions"] = True
        save()
        return _out(
            _local(
                language,
                "Understood. I’ll stop extra suggestions for this cart. "
                "Tell me what you need next.",
                "ठीक है। इस कार्ट के लिए अतिरिक्त सुझाव रोक दूँगा। आगे क्या चाहिए?",
                "Theek hai. Is cart ke liye extra suggestions band. Aage kya chahiye?",
            ),
            "suggestions_declined",
        )
    if kind == "done":
        return _out(_finish(language), "ready_to_review")
    if kind == "confused":
        return _out(
            _local(
                language,
                "Is a lower price more important, or a larger pack?",
                "कम कीमत ज़रूरी है या बड़ा पैक?",
                "Kam price important hai ya larger pack?",
            ),
            "clarify_priority",
        )
    if kind in ("price", "pack"):
        state["priority"] = kind
        save()
        if kind == "pack":
            return _out(
                _local(
                    language,
                    "What pack size would you prefer? I’ll keep the "
                    "product requirements unchanged.",
                    "कितना बड़ा पैक चाहिए? उत्पाद की बाकी शर्तें वही रहेंगी।",
                    "Kitna bada pack chahiye? Product ki baaki shartein same rahengi.",
                ),
                "clarify_pack_size",
            )
    needs = state.get("needs", [])
    if not needs or state.get("unverified_requirements"):
        return _out(
            _local(
                language,
                "Which product and essential requirements should I keep?",
                "कौन सा उत्पाद और कौन सी ज़रूरी शर्तें रखूँ?",
                "Kaunsa product aur zaroori shartein same rakhun?",
            ),
            "clarify_requirements",
        )
    budget = stated_budget(message) or state.get("budget")
    rejected = state.get("rejected", [])
    hits, lines = [], []
    replacements = []
    current = {line["sku"]: line["quantity"] for line in (cart.lines if cart else [])}
    for original_need in needs[:4]:
        need = dict(original_need)
        bound = [sku for sku in need.get("skus", []) if sku in current]
        if len(bound) > 1:
            return _out(
                _local(
                    language,
                    "Which cart item should I replace?",
                    "कार्ट में कौन सा उत्पाद बदलें?",
                    "Cart ka kaunsa item replace karein?",
                ),
                "clarify_replacement",
            )
        if bound:
            replacements.extend(bound)
            need["quantity"] = current[bound[0]]
            source = tools.call("catalog.get_product", sku=bound[0])
            if not source.ok:
                return _out(_finish(language), "quote_unavailable")
            # A cheaper replacement must not quietly shrink the already selected pack.
            need["required_terms"] = [*need.get("required_terms", []), source.payload["unit_label"]]
        candidates = _matching(need, tools, rejected)
        if bound:
            candidates = [
                hit for hit in candidates if hit["category"] == source.payload["category"]
            ]
        candidates.sort(key=lambda h: h["unit_price_minor"])
        if not candidates:
            return _out(
                _local(
                    language,
                    "No available alternative matches all those "
                    "requirements. Which requirement may we change?",
                    "सभी शर्तें पूरी करने वाला उपलब्ध विकल्प नहीं मिला। कौन सी शर्त बदल सकते हैं?",
                    "Saari shartein match karta available alternative "
                    "nahi mila. Kaunsi shart badal sakte hain?",
                ),
                "no_matching_alternative",
            )
        hit = candidates[0]
        hits.append(hit)
        lines.append({"sku": hit["sku"], "quantity": need.get("quantity", 1)})
    if len({line["sku"] for line in lines}) != len(lines):
        return _out(_finish(language), "clarify_overlapping_items")
    # A replacement preview includes unaffected cart items. Never call a unit-price sum a bill.
    base = {line["sku"]: line["quantity"] for line in (cart.lines if cart else [])}
    for sku in replacements:
        base.pop(sku, None)
    for line in lines:
        base[line["sku"]] = line["quantity"]
    if len(base) > 4:
        return _out(
            _local(
                language,
                "Please choose one item to change so I can verify its full basket total.",
                "एक उत्पाद चुनें ताकि पूरे कार्ट का नया कुल जाँच सकूँ।",
                "Ek item choose karein taaki full basket total verify kar sakun.",
            ),
            "clarify_replacement",
        )
    # Unaffected items also need a fresh observed product before the preview tool
    # accepts them. Merely reading their identifiers from a cart is not price evidence.
    for sku in base:
        if sku not in tools.ledger.seen_skus:
            checked = tools.call("catalog.get_product", sku=sku)
            if not checked.ok:
                return _out(
                    _local(
                        language,
                        "I cannot verify the complete basket yet.",
                        "पूरे कार्ट की पुष्टि अभी नहीं हो पाई।",
                        "Poora basket abhi verify nahi hua.",
                    ),
                    "quote_unavailable",
                )
    result = tools.call(
        "cart.preview", lines=[{"sku": sku, "quantity": qty} for sku, qty in base.items()]
    )
    if not result.ok or not result.payload.get("ok"):
        return _out(
            _local(
                language,
                "I cannot verify a fresh total yet. Your cart is unchanged.",
                "नया कुल अभी सत्यापित नहीं हुआ। कार्ट नहीं बदला है।",
                "Fresh total verify nahi hua. Cart same hai.",
            ),
            "quote_unavailable",
        )
    quote = dict(result.payload)
    fits = (
        None
        if not budget
        else (
            quote["total"]["minor"] <= budget[0]
            if budget[1]
            else quote["total"]["minor"] < budget[0]
        )
    )
    quote["within_stated_budget"] = fits
    text = _local(
        language,
        f"Lowest-priced matching available packs shown. Preview total "
        f"{quote['total']['display']} including tax and delivery.",
        f"मिलते हुए उपलब्ध पैक में सबसे कम दाम वाले विकल्प दिख रहे हैं। "
        f"टैक्स और डिलीवरी सहित कुल {quote['total']['display']}।",
        f"Matching available packs mein lowest-price options dikh rahe "
        f"hain. Tax aur delivery ke saath total "
        f"{quote['total']['display']}.",
    )
    if fits is False:
        text += _local(
            language,
            " This still exceeds your budget. May we change the pack size or brand?",
            " यह अभी भी बजट से अधिक है। पैक या ब्रांड बदल सकते हैं?",
            " Ye abhi bhi budget se zyada hai. Pack ya brand badal sakte hain?",
        )
    else:
        text += _local(
            language,
            " Shall we consider these? Your cart is unchanged.",
            "ये विकल्प देखें? कार्ट नहीं बदला है।",
            "Ye options dekhein? Cart nahi badla hai.",
        )
    state["last_suggestion"] = [h["sku"] for h in hits]
    save()
    return _out(text, "alternatives_ready", hits, preview_quote=quote)


def remember(
    session: Session,
    ctx: RequestContext,
    cart_id: uuid.UUID | None,
    outcome: TurnOutcome,
    message: str,
) -> None:
    if not cart_id:
        return
    structured = outcome.structured or {}
    hits = structured.get("hits", [])
    if not hits:
        return
    cart = cart_service.load_cart(session, ctx, cart_id, lock=True)
    session.refresh(cart, attribute_names=["shopping_context"])
    state = dict(cart.shopping_context or {})
    state["displayed"] = [h["sku"] for h in hits][:5]
    plan = structured.get("plan")
    if plan:
        state["needs"] = [
            dict(
                need,
                skus=next(
                    (
                        group["skus"]
                        for group in structured.get("groups", [])
                        if group["query"] == need["query"]
                    ),
                    [],
                ),
            )
            for need in plan["needs"]
        ]
        state["unverified_requirements"] = plan.get("unverified_requirements", [])
    elif not structured.get("sales_status"):
        from .discovery import discovery_query

        query = discovery_query(message)
        if query:
            state["needs"] = [{"query": query, "quantity": 1, "skus": state["displayed"]}]
            state["unverified_requirements"] = []
    budget = stated_budget(message)
    if budget:
        state["budget"] = list(budget)
    elif not structured.get("sales_status"):
        state.pop("budget", None)
    cart.shopping_context = state
    session.flush()

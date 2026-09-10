"""Bounded planning and fresh retrieval; no cart writes or payment authority."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_service import TurnOutcome

from .discovery import discovery_query
from .search_constraints import _terms


def is_followup(message: str) -> bool:
    return bool(
        re.search(
            r"^(replace|instead|remove|swap|change)\b|\b(ki jagah|ke badle|badlo)\b"
            r"|की जगह|के बदले|बदलो",
            message.casefold().strip(),
        )
    )


def eligible_request(message: str, previous: dict[str, Any] | None) -> bool:
    text = message.casefold()
    if re.search(r"\b(pay|refund|checkout|approve|buy)\b|भुगतान|रिफंड", text):
        return False
    return bool(
        re.search(
            r"\b(compare|comparison|alternatives|alternative|recommend|recommendation|breakfast|party|picnic|bundle|meal|nashta|nashte|naashta|tulna)\b|तुलना|नाश्ता|नाश्ते|पार्टी|बंडल",
            text,
        )
        or (previous and is_followup(text))
    )


def comparison_plan(message: str) -> dict[str, Any] | None:
    match = re.fullmatch(
        r"compare (.+?) (?:and|versus|vs) (.+?)(?: for breakfast)?[.!?]*",
        message.casefold().strip(),
    )
    if not match:
        match = re.fullmatch(
            r"(.+?) (?:aur|और) (.+?) (?:compare karo|ki tulna karo|की तुलना करो|की तुलना करें)[.!?।]*",
            message.casefold().strip(),
        )
    if not match:
        return None
    queries = [discovery_query(part) for part in match.groups()]
    if not all(queries):
        return None
    return {
        "mode": "compare",
        "needs": [
            {"query": q, "quantity": 1, "required_terms": [], "excluded_terms": []} for q in queries
        ],
        "clarification": "",
        "unverified_requirements": [],
    }


def replacement_plan(message: str, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """Update an unambiguous discovery-plan group; never remove or add cart lines."""
    import copy

    match = re.fullmatch(
        r"(?:replace|swap) (.+?) (?:with|for) (.+?)[.!।]*"
        r"|(.+?) (?:ki jagah|ke badle|की जगह|के बदले) (.+?)[.!।]*",
        message.casefold().strip(),
    )
    if not match or previous is None:
        return None
    old, new = (match[1], match[2]) if match[1] else (match[3], match[4])
    old_query, new_query = discovery_query(old), discovery_query(new)
    if not old_query or not new_query:
        return None
    saved_plan = previous.get("plan", {})
    if not isinstance(saved_plan, dict):
        return None
    plan: dict[str, Any] = copy.deepcopy(saved_plan)
    matches = [
        need for need in plan.get("needs", []) if _terms(old_query).issubset(_terms(need["query"]))
    ]
    if len(matches) != 1:
        # An unanswered clarification is not a selected bundle. Do not let the model
        # invent the missing original selection merely because the user says replace.
        return {
            "mode": "clarify",
            "needs": [],
            "clarification": "",
            "unverified_requirements": plan.get("unverified_requirements", []),
        }
    # Keep quantity, exclusions and all explicit requirements. Conflicting requirements
    # produce an incomplete preview rather than being silently discarded.
    matches[0]["query"] = new_query
    return plan


def stated_budget(message: str) -> tuple[int, bool] | None:
    message = unicodedata.normalize("NFKC", message).casefold()
    message = "".join(str(unicodedata.digit(c)) if c.isdecimal() else c for c in message)
    # Canonicalize valid grouped amounts before parsing; a comma may never truncate money.
    message = re.sub(
        r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])", lambda m: m[0].replace(",", ""), message
    )
    message = re.sub(
        r"(?:₹\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:rupees?|rupaye|रुपये|रुपए))\s*(?:mein|me|में)(?=\s|$)",
        lambda m: "within " + (m[1] or m[2]),
        message,
    )
    # Translate only explicit numeric budget grammar; never ask a model to do arithmetic.
    message = re.sub(
        r"₹?\s*(\d+(?:\.\d{1,2})?)\s*(?:rupees?|rupaye|rs|रुपये|रुपए)?\s*"
        r"(ke andar|se kam|तक|के अंदर|से कम)",
        lambda m: " " + ("under " if m[2] in ("se kam", "से कम") else "within ") + m[1] + " ",
        message,
    )
    matches = list(
        re.finditer(
            r"\b(under|below|up to|within)\s*₹?\s*(\d+(?:\.\d{1,2})?)(?![\d.,])", message.casefold()
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    return int(Decimal(match[2]) * 100), match[1] in ("up to", "within")


def execute(
    plan: dict[str, Any],
    tools: Any,
    language: str,
    budget: tuple[int, bool] | None,
    requirements_context: str = "",
) -> TurnOutcome:
    from agent_runtime.runtime_adk.shopping_plan import ShoppingPlan

    from .agent_service import TurnOutcome

    parsed = ShoppingPlan.model_validate(plan)
    unverifiable = any(
        re.search(
            r"allerg|allergen|peanut.free|nut.free|gluten.free|vegetarian|vegan|शाकाहारी|एलर्जी",
            requirement.casefold(),
        )
        for requirement in [*parsed.unverified_requirements, requirements_context]
    )
    if unverifiable:
        text = {
            "hi": "कैटलॉग में एलर्जी या आहार संबंधी शर्तों की पुष्टि करने वाली जानकारी नहीं है। "
            "इसलिए इन्हें पूरा करने वाला उत्पाद अभी नहीं सुझा सकता। "
            "किस उत्पाद की पैकेजिंग जानकारी जाँचनी है?",
            "hi-Latn": "Catalogue mein allergy ya dietary requirements verify karne ki information "
            "nahi hai. Isliye matching product abhi recommend nahi kar sakta. "
            "Kis product ki packaging information check karni hai?",
        }.get(
            language,
            "The catalogue cannot verify your allergy or dietary requirements, "
            "so I cannot recommend a matching product yet. "
            "Which product's packaging information would you like to check?",
        )
        return TurnOutcome(
            reply=text,
            structured={
                "kind": "products",
                "hits": [],
                "planning_status": "constraints_unverified",
                "unverified_requirements": parsed.unverified_requirements,
            },
        )
    if parsed.mode == "clarify" or not parsed.needs:
        question = {
            "hi": "कौन से उत्पाद पसंद करेंगे, और आपके पास पहले से क्या उपलब्ध है?",
            "hi-Latn": "Kaunse products pasand karenge, aur aapke paas pehle se kya hai?",
        }.get(
            language, "Which products would you prefer, and which ingredients do you already have?"
        )
        return TurnOutcome(
            reply=question,
            structured={
                "kind": "products",
                "hits": [],
                "planning_status": "clarification_required",
            },
        )
    selected: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    missing: list[str] = []
    searches: dict[str, Any] = {}
    for need in parsed.needs:
        # ToolExecutor holds a SQLAlchemy session/ledger; never share it across threads.
        # Reuse identical reads within this turn only, keeping the next turn fresh.
        key = " ".join(need.query.casefold().split())
        if key not in searches:
            searches[key] = tools.call("catalog.search", query=need.query, limit=50)
        result = searches[key]
        if not result.ok:
            missing.append(need.query)
            continue
        required = set().union(*(_terms(t) for t in need.required_terms))
        excluded = [_terms(t) for t in need.excluded_terms if t.strip()]
        candidates = []
        for hit in result.payload["hits"]:
            names = _terms(
                " ".join(str(hit.get(k, "")) for k in ("name_en", "name_hi", "display_name"))
            )
            if (
                not hit.get("is_available")
                or not required.issubset(names)
                or any(terms.issubset(names) for terms in excluded)
            ):
                continue
            candidates.append(hit)
        # Exact name terms dominate accessories; don't substitute a weak match silently.
        direct = [
            h
            for h in candidates
            if _terms(need.query).issubset(
                _terms(" ".join(str(h.get(k, "")) for k in ("name_en", "name_hi", "display_name")))
            )
        ]
        candidates = direct or candidates
        if parsed.mode == "compare" and budget:
            candidates = [
                h
                for h in candidates
                if (
                    h["unit_price"]["minor"] <= budget[0]
                    if budget[1]
                    else h["unit_price"]["minor"] < budget[0]
                )
            ]
        if parsed.mode == "bundle":
            candidates = [h for h in candidates if h.get("stock_units", 0) >= need.quantity]
            if budget:
                candidates.sort(key=lambda h: h["unit_price"]["minor"])
        picked = candidates[: 2 if parsed.mode == "compare" else 1]
        if not picked:
            missing.append(need.query)
        groups.append(
            {"query": need.query, "skus": [h["sku"] for h in picked], "quantity": need.quantity}
        )
        for hit in picked:
            if not any(h["sku"] == hit["sku"] for h in selected):
                selected.append(hit)
            if parsed.mode == "bundle":
                if any(line["sku"] == hit["sku"] for line in lines):
                    missing.append("overlapping selections need clarification")
                else:
                    lines.append({"sku": hit["sku"], "quantity": need.quantity})
    preview = None
    if parsed.mode == "bundle" and budget and lines and not missing:
        result = tools.call("cart.preview", lines=lines)
        if result.ok:
            preview = dict(result.payload)
            if preview.get("ok"):
                total = preview["total"]["minor"]
                preview["within_stated_budget"] = (
                    total <= budget[0] if budget[1] else total < budget[0]
                )

    def local(en: str, hi: str, hinglish: str) -> str:
        return {"hi": hi, "hi-Latn": hinglish}.get(language, en)

    facts = []
    for group in groups:
        rows = [h for h in selected if h["sku"] in group["skus"]]
        if rows:
            facts.append(
                "; ".join(f"{h['display_name']} — {h['unit_price']['display']} INR" for h in rows)
                + "."
            )
    if preview and preview.get("ok"):
        fits = preview["within_stated_budget"]
        facts.append(
            local(
                f"Preview for the selected pack quantities: {preview['total']['display']} "
                f"including current tax and delivery; {'within' if fits else 'above'} "
                "your stated budget.",
                f"चुने हुए पैक का कुल {preview['total']['display']}, टैक्स और डिलीवरी सहित; "
                f"आपके बजट {'के अंदर' if fits else 'से अधिक'} है।",
                f"Selected packs ka total {preview['total']['display']}, "
                "tax aur delivery ke saath; "
                f"aapke budget {'ke andar' if fits else 'se zyada'} hai.",
            )
        )
    elif parsed.mode == "bundle":
        facts.append(
            local(
                "These are starting options, not a verified complete-basket budget.",
                "ये शुरुआती विकल्प हैं; पूरे बास्केट का बजट अभी सत्यापित नहीं है।",
                "Ye starting options hain; poore basket ka budget abhi verify nahi hua.",
            )
        )
    if parsed.mode == "compare":
        facts.append(
            local(
                "Compare the listed pack sizes and current prices.",
                "पैक के आकार और वर्तमान दाम की तुलना करें।",
                "Pack sizes aur current prices compare karein.",
            )
        )
    if missing:
        facts.append(
            local(
                "Some requested items could not be matched; the plan is incomplete.",
                "कुछ मांगे गए उत्पाद नहीं मिले; चयन अधूरा है।",
                "Kuch requested products nahi mile; selection adhura hai.",
            )
        )
    unknown = list(parsed.unverified_requirements)
    if preview and preview.get("ok"):
        # Only discharge a plain budget requirement matching the evaluated amount.
        # Mixed requirements (e.g. gluten-free under 300) must remain unverified.
        unknown = [
            requirement
            for requirement in unknown
            if not (
                re.fullmatch(
                    r"(?:total (?:price|cost|budget)|budget)?\s*(?:under|below|within|up to)"
                    r"\s*₹?\s*\d+(?:\.\d{1,2})?\s*(?:rupees|inr)?[.]?",
                    requirement.strip().casefold(),
                )
                and (evaluated := stated_budget(requirement)) is not None
                and budget is not None
                and evaluated[0] == budget[0]
            )
        ]
    if parsed.mode == "bundle":
        facts.append(
            local(
                "Portions and pantry ingredients still need checking; "
                "no cart items have been added.",
                "मात्रा और घर में उपलब्ध सामग्री जाँचना बाकी है; कार्ट में कुछ नहीं जोड़ा है।",
                "Servings aur ghar ki ingredients check karna baaki hai; "
                "cart mein kuch add nahi kiya.",
            )
        )
    if unknown:
        facts.append(
            local(
                "Some requested suitability requirements still need verification.",
                "आपकी कुछ शर्तें अभी सत्यापित नहीं हैं।",
                "Aapki kuch requirements abhi verify nahi hui hain.",
            )
        )
    facts.append(
        local(
            "The lowest-priced matching packs still exceed your budget. "
            "May we change the pack size or brand?"
            if preview and preview.get("within_stated_budget") is False
            else "Which option or quantity would you like to change?",
            "मिलते हुए सबसे सस्ते पैक भी बजट से अधिक हैं। पैक या ब्रांड बदल सकते हैं?"
            if preview and preview.get("within_stated_budget") is False
            else "कौन सा विकल्प या मात्रा बदलें?",
            "Matching cheapest packs bhi budget se zyada hain. Pack ya brand badal sakte hain?"
            if preview and preview.get("within_stated_budget") is False
            else "Kaunsa option ya quantity badlein?",
        )
    )
    return TurnOutcome(
        reply=" ".join(facts),
        structured={
            "kind": "products",
            "hits": selected,
            "groups": groups,
            "preview_quote": preview,
            "planning_status": "incomplete" if missing else "options_ready",
            "unverified_requirements": unknown,
            "missing": missing,
            "selection_basis": "lowest_pack_price_among_matches"
            if budget and parsed.mode == "bundle"
            else "catalogue_relevance",
            "plan": parsed.model_dump(),
            "model_rounds": 0,
        },
    )

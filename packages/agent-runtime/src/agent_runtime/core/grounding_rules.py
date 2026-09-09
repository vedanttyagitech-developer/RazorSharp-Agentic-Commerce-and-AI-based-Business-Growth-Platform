"""Grounding rules: prevention before the model, not only detection after it.

A rule reads the buyer's message and names the ONE read tool the turn must start with.
"Is milk still ₹28?" starts from ``basket_get``; "where is order 0192..." starts from
``order_track``; a message naming a SKU the session has never seen starts from
``product``. The answer to a question of that shape then begins from a tool result, and
the post-check in ``grounding/postcheck.py`` has something to check against instead of
having to drop what the model invented.

HOW A RULE IS ENFORCED
----------------------
Two ways, chosen per rule by whether the harness already knows the tool's input:

* **prefetch** -- the harness runs the read itself through the same gated tool and puts
  the fenced result above the message. Used where the input is known: a SKU token in the
  text, the session's cart or checkout id, an order id. A rule with a
  ``prefetch_intro`` is a prefetch rule.
* **force** -- the adapter pins round one to the tool (``FunctionCallingConfig`` mode
  ``ANY`` with one allowed name) and the model writes the arguments. Used only where the
  input is the model's to write: a free-text terms query. A rule without a
  ``prefetch_intro`` is a force-only rule.

The harness never routes on model prose. Lexicons are configuration -- English, Hindi
and Hinglish terms sit in one tuple because a buyer mixes them in one sentence -- and
this module only matches. Rules are precedence-ordered; the first that fires wins.

Matching primitives follow ``commerce_common/grounding.py`` in anthropics/commerce-agents
(Apache-2.0), extended with a Devanagari-aware word boundary and rupee literals.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Final

__all__ = [
    "CHECKOUT_RULES",
    "DEFAULT_LEXICON",
    "SHOPPING_RULES",
    "SUPPORT_RULES",
    "GroundingLexicon",
    "GroundingRule",
    "GroundingState",
    "RuleInput",
    "find_token",
    "first_rule",
    "fold",
    "forced_tool",
    "matches_any",
    "matches_terms_and_cues",
    "rules_for",
]

RuleInput = dict[str, Any]

# A concrete money or percent figure counts as a change term on its own.
_MONEY_LITERAL: Final[re.Pattern[str]] = re.compile(
    r"(?:₹|Rs\.?|INR|रु\.?|रुपये|रूपये)\s?\d|\d+\s?(?:rupees?|rupaye|rupiye|रुपये|रूपये)",
    re.IGNORECASE,
)
_PERCENT_LITERAL: Final[re.Pattern[str]] = re.compile(r"\d+\s?(?:%|percent|pratishat|प्रतिशत)")

#: Devanagari letters and marks count as word characters. Python's ``\\b`` treats a vowel
#: sign (category Mn/Mc) as a non-word character, so ``\\bनहीं\\b`` could never match at
#: the end of a sentence: the anusvara that ends the word is "outside" the word to ``\\b``.
_WORDISH: Final[str] = r"[\wऀ-ॿ꣠-ꣿ]"


@cache
def _needle_pattern(needle: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!{_WORDISH}){re.escape(needle)}(?!{_WORDISH})", re.IGNORECASE)


def normalize(text: str) -> str:
    """NFKC alone, preserving case: the shape a pattern should be matched against.

    Used where the caller needs the matched substring back as the buyer typed it, so the
    identifier it names survives unchanged. ``fold`` adds casefolding for term matching,
    where nothing is returned and only the yes/no matters.
    """
    return unicodedata.normalize("NFKC", text)


def fold(text: str) -> str:
    """NFKC, then casefold: one spelling to match against, whatever the keyboard emitted.

    Casefolding alone is not a defence in this market. A Hindi IME emits the *precomposed*
    nukta letters (``क़`` U+0958, ``ज़`` U+095B); this file's lexicons are written with the
    decomposed pair (``क`` + U+093C) that NFKC canonicalises to, and the two are different
    strings. Fullwidth Latin (``ｐｒｉｃｅ``) folds to ASCII for the same reason. Without
    this, "क़ीमत क्या है?" matched no term, no read was forced, and the model answered a
    price question from memory -- the exact failure these rules exist to prevent.
    """
    return normalize(text).casefold()


def matches_any(text: str, needles: Sequence[str]) -> bool:
    """Case-insensitive whole-word (or whole-phrase) match; ``?`` matches literally.

    Whole-word means "fee" does not fire on "coffee" and "terms" does not fire on
    "determines". A phrase needle ("how much") must appear as those words in that order.
    Both sides are folded through :func:`fold`, so the comparison does not depend on which
    keyboard produced the text.
    """
    lowered = fold(text)
    for needle in needles:
        cleaned = fold(needle).strip()
        if not cleaned:
            continue
        if cleaned == "?":
            if "?" in lowered:
                return True
        elif _needle_pattern(cleaned).search(lowered):
            return True
    return False


def matches_terms_and_cues(
    text: str, terms: Sequence[str], cues: Sequence[str], *, numeric_literals: bool = False
) -> bool:
    """True when the text carries a term *and* a cue.

    With ``numeric_literals`` a rupee or percent figure counts as a term on its own, so
    "take it from ₹29 to ₹26" is a price question without the word "price". Empty text
    or an empty lexicon never fires: a rule with nothing configured must not force a
    tool on every turn.
    """
    if not text or not terms or not cues or not matches_any(text, cues):
        return False
    if matches_any(text, terms):
        return True
    if not numeric_literals:
        return False
    folded = fold(text)
    return bool(_MONEY_LITERAL.search(folded) or _PERCENT_LITERAL.search(folded))


def find_token(text: str, patterns: Sequence[str]) -> str | None:
    """The longest match of any pattern in ``text`` (case-insensitive), or None.

    Longest wins because id families nest: ``SKU-TIX-104-GA`` contains ``SKU-TIX-104``,
    and the buyer meant the one they typed in full.
    """
    token: str | None = None
    # NFKC, not fold: the matched substring is returned and becomes an identifier, so
    # its case must survive. The patterns already carry ``re.IGNORECASE``.
    normalized = normalize(text) if text else ""
    for pattern in patterns if normalized else ():
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match is not None and (token is None or len(match.group(0)) > len(token)):
            token = match.group(0)
    return token


# ------------------------------------------------------------------------------ config


@dataclass(frozen=True, slots=True)
class GroundingLexicon:
    """The words the rules look for. Configuration, not logic; three languages per tuple.

    Hindi entries are Devanagari; Hinglish entries are the romanisations a buyer actually
    types. Where a Hinglish word is also an English word ("order", "cancel") it appears
    once and serves both.
    """

    #: Catalogue identifiers look like ``AMUL-DAIRY-001``.
    sku_patterns: tuple[str, ...] = (r"\b[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}\b",)
    #: How an order can be named in a sentence: on the wire, and out loud.
    #:
    #: The UUID is what a client sends. The ``RS-260909-XW5G26M`` is what every screen
    #: shows, what the copilot says, and what a buyer reads back over the phone -- and for
    #: a long time it was the only form nobody could look up, so "mera order
    #: RS-260909-XW5G26M kahan hai" matched nothing and the model answered a tracking
    #: question with no tracking data.
    #:
    #: Read case-insensitively and with the hyphens optional, because a buyer types what
    #: they can see. The tail excludes I, L, O and U for the same reason
    #: ``commerce_domain.ids`` excludes them: they are not in the alphabet, so their
    #: presence is a typo rather than a character to guess at.
    order_id_patterns: tuple[str, ...] = (
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        r"\bRS-?\d{6}-?[0-9A-HJKMNP-TV-Z]{7}\b",
    )

    #: A question about the cart's own numbers.
    cart_terms: tuple[str, ...] = (
        "price", "prices", "cost", "total", "subtotal", "quantity", "how many", "how much",
        "cart", "cart", "bill", "amount",
        "kitna", "kitne", "kitni", "daam", "dam", "keemat", "kimat", "paisa", "paise",
        "total kya", "cart mein", "cart mein",
        "कीमत", "क़ीमत", "दाम", "कुल", "कितना", "कितने", "कितनी", "टोकरी", "बिल", "राशि",
    )  # fmt: skip
    #: Delivery and fee terms, for the checkout specialist.
    delivery_terms: tuple[str, ...] = (
        "delivery", "delivery fee", "delivery charge", "shipping", "free delivery", "fee",
        "fees", "charge", "charges", "tax", "gst",
        "shulk", "shipping charge", "free delivery ke liye", "delivery charge kitna",
        "डिलीवरी", "डिलिवरी", "शुल्क", "टैक्स", "जीएसटी", "मुफ़्त डिलीवरी", "मुफ्त डिलीवरी",
    )  # fmt: skip
    #: Post-purchase: an order by cue rather than by id.
    order_terms: tuple[str, ...] = (
        "order", "orders", "my order", "delivery status", "track", "tracking", "where is",
        "status", "arrived", "delivered", "late",
        "mera order", "order kahan", "kab aayega", "kab ayega", "pahuncha", "pahucha",
        "ऑर्डर", "आर्डर", "ऑर्डर कहाँ", "कब आएगा", "पहुँचा", "स्थिति",
    )  # fmt: skip
    #: A remedy request: refund, cancel, return, replace.
    remedy_terms: tuple[str, ...] = (
        "refund", "refunds", "cancel", "cancellation", "return", "replace", "replacement",
        "money back", "wrong item", "damaged", "missing",
        "paise wapas", "paisa wapas", "wapas", "cancel karo", "cancel kardo", "kharab",
        "toota", "tuta", "galat item", "nahi aaya", "nahi mila",
        "धनवापसी", "रिफंड", "रद्द", "वापस", "पैसे वापस", "ख़राब", "खराब", "टूटा", "गलत",
        "नहीं आया", "नहीं मिला",
    )  # fmt: skip
    #: A cue that the sentence is a question or a request, not a statement.
    question_cues: tuple[str, ...] = (
        "?", "what", "what's", "whats", "how", "how much", "how many", "is", "are", "does",
        "do", "can", "could", "will", "would", "why", "when", "where", "which", "show",
        "tell", "tell me", "check", "still", "yet",
        "kya", "kyu", "kyun", "kyon", "kaise", "kab", "kahan", "kaha", "kaunsa", "kaun",
        "kitna", "kitne", "kitni", "batao", "bataiye", "batana", "dikhao", "dikhaiye",
        "check karo", "dekho", "abhi bhi",
        "क्या", "क्यों", "कैसे", "कब", "कहाँ", "कहां", "कौन", "कौनसा", "बताओ", "बताइए",
        "दिखाओ", "दिखाइए", "देखो", "अभी भी",
    )  # fmt: skip
    #: A cue that the buyer wants something *done*, for the remedy rule.
    action_cues: tuple[str, ...] = (
        "?", "want", "need", "please", "can", "could", "how", "get", "give", "start", "i want",
        "chahiye", "karo", "kardo", "kar do", "kijiye", "karna", "karwa", "karvana",
        "milega", "milegi", "dilao", "do",
        "चाहिए", "करो", "कर दो", "कीजिए", "करना", "मिलेगा", "मिलेगी", "दिलाओ",
    )  # fmt: skip


DEFAULT_LEXICON: Final[GroundingLexicon] = GroundingLexicon()


# ------------------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class GroundingState:
    """What a rule may know about the session. Built by the harness; never by a model.

    ``seen_skus`` comes from the session provenance record, so an id the session has
    already resolved never forces a re-read; ids compare case-insensitively.
    """

    seen_skus: Collection[str] = field(default_factory=frozenset)
    cart_id: str | None = None
    checkout_id: str | None = None
    order_id: str | None = None

    def has_seen_sku(self, sku: str) -> bool:
        wanted = sku.upper()
        return any(seen.upper() == wanted for seen in self.seen_skus)


FiresFn = Callable[[GroundingLexicon, str, GroundingState], "RuleInput | None"]
IntroFn = Callable[[RuleInput], str]


@dataclass(frozen=True, slots=True)
class GroundingRule:
    """One rule: ``fires(lexicon, text, state)`` returns the tool's input or None.

    ``prefetch_intro`` renders the line a prefetching harness puts above the tool result.
    A rule without one is honoured only where the adapter can force the tool, because
    its input is the model's to write.
    """

    name: str
    tool: str
    fires: FiresFn
    prefetch_intro: IntroFn | None = None

    @property
    def prefetchable(self) -> bool:
        return self.prefetch_intro is not None


def first_rule(
    rules: Sequence[GroundingRule], lexicon: GroundingLexicon, text: str, state: GroundingState
) -> tuple[GroundingRule, RuleInput] | None:
    """The first rule in precedence order that fires, with the input it computed."""
    for rule in rules:
        args = rule.fires(lexicon, text, state)
        if args is not None:
            return rule, args
    return None


def forced_tool(
    rules: Sequence[GroundingRule], lexicon: GroundingLexicon, text: str, state: GroundingState
) -> str | None:
    """The tool the turn's first round is pinned to, by rule precedence."""
    found = first_rule(rules, lexicon, text, state)
    return None if found is None else found[0].tool


# ---------------------------------------------------------------------- the P0 rules
#
# Tool names here are the names the factory in ``capabilities/tools.py`` registers. The
# harness resolves a rule's tool by name against the bound toolset, so a rule naming a
# tool a specialist does not hold simply cannot be prefetched -- there is nothing to call.


def _catalogue(lexicon: GroundingLexicon, text: str, state: GroundingState) -> RuleInput | None:
    """A SKU the session has not seen: read it before the model can describe it."""
    token = find_token(text, lexicon.sku_patterns)
    if token is None or state.has_seen_sku(token):
        return None
    return {"sku": token.upper()}


def _cart_numbers(lexicon: GroundingLexicon, text: str, state: GroundingState) -> RuleInput | None:
    """A quantity or price question with a cart in session: re-quote first."""
    if state.cart_id is None:
        return None
    if not matches_terms_and_cues(
        text, lexicon.cart_terms, lexicon.question_cues, numeric_literals=True
    ):
        return None
    return {"cart_id": state.cart_id}


def _checkout_state(_: GroundingLexicon, __: str, state: GroundingState) -> RuleInput | None:
    """Any turn with a checkout in session starts from its current version and status.

    Unconditional on purpose: prices move under a checkout (that is the demonstration),
    and a checkout specialist that answers from last turn's card is the failure this
    package exists to prevent.
    """
    if state.checkout_id is None:
        return None
    return {"checkout_id": state.checkout_id}


def _delivery_or_fee(
    lexicon: GroundingLexicon, text: str, state: GroundingState
) -> RuleInput | None:
    """Delivery or fee terms with a cue: the fee engine's numbers, never the model's.

    Prefetched rather than forced (ADR 0004 table said forced): ``basket_get`` takes a
    cart id, which the harness knows and the model must not write. With no cart in
    session there is nothing to re-quote and the rule stays silent.
    """
    if state.cart_id is None:
        return None
    if not matches_terms_and_cues(
        text, lexicon.delivery_terms, lexicon.question_cues, numeric_literals=True
    ):
        return None
    return {"cart_id": state.cart_id}


def _order(lexicon: GroundingLexicon, text: str, state: GroundingState) -> RuleInput | None:
    """An order id in the text, or an order cue with an order in session: read it first."""
    token = find_token(text, lexicon.order_id_patterns)
    if token is not None:
        return {"order_id": token.lower()}
    if state.order_id is not None and matches_terms_and_cues(
        text, lexicon.order_terms, lexicon.question_cues
    ):
        return {"order_id": state.order_id}
    return None


def _remedy(lexicon: GroundingLexicon, text: str, state: GroundingState) -> RuleInput | None:
    """Refund or cancel terms with an action cue: the Resolution Service decides what is owed.

    Force-only: the model writes the evaluation request (which order, what the buyer
    reports), and an amount may only ever come from the plan the service returns.
    """
    if state.order_id is None:
        return None
    if not matches_terms_and_cues(text, lexicon.remedy_terms, lexicon.action_cues):
        return None
    return {}


def _sku_intro(args: RuleInput) -> str:
    return (
        f"Catalogue record for {args['sku']}, fetched by the platform before this turn "
        "(the same data a product call returns):"
    )


def _cart_intro(_: RuleInput) -> str:
    return (
        "The cart as the fee engine prices it right now, fetched by the platform before "
        "this turn (the same data a basket_get call returns):"
    )


def _checkout_intro(_: RuleInput) -> str:
    return (
        "The checkout's current version and status, fetched by the platform before this "
        "turn (the same data a checkout_get call returns):"
    )


def _order_intro(args: RuleInput) -> str:
    return (
        f"Order {args['order_id']} as the platform records it, fetched before this turn "
        "(the same data an order_track call returns):"
    )


SHOPPING_RULES: Final[tuple[GroundingRule, ...]] = (
    GroundingRule("catalogue", "product", _catalogue, prefetch_intro=_sku_intro),
    GroundingRule("cart", "basket_get", _cart_numbers, prefetch_intro=_cart_intro),
)

CHECKOUT_RULES: Final[tuple[GroundingRule, ...]] = (
    GroundingRule("state", "checkout_get", _checkout_state, prefetch_intro=_checkout_intro),
    GroundingRule("delivery_or_fee", "basket_get", _delivery_or_fee, prefetch_intro=_cart_intro),
)

SUPPORT_RULES: Final[tuple[GroundingRule, ...]] = (
    GroundingRule("order", "order_track", _order, prefetch_intro=_order_intro),
    GroundingRule("remedy", "resolution_evaluate", _remedy),
)

_RULES_BY_ROLE: Final[dict[str, tuple[GroundingRule, ...]]] = {
    "shopping": SHOPPING_RULES,
    "checkout": CHECKOUT_RULES,
    "support": SUPPORT_RULES,
}


def rules_for(role: str) -> tuple[GroundingRule, ...]:
    """The precedence-ordered rules for one specialist role; empty for an unknown role.

    Empty rather than an error: a role with no rules gets no forced first tool, which is
    the safe default -- a read-only specialist that presents what it is handed needs none.
    """
    return _RULES_BY_ROLE.get(role, ())

"""The outbound content guard (19.9, 19.10): what a model is allowed to say aloud.

The model may converse about products, comparisons and choices. It does not author speech
about approvals, totals, deltas, reservation expiry, payment outcomes, cancellation
effects, refunds or delegated authority. Those sentences are rendered from versioned
templates filled with server-confirmed fields and carry ``deterministic=True``. A
model-authored sentence that states such a fact is refused **before synthesis**, which is
the whole reason the pipeline is split: text exists before speech, so a sentence can be
refused rather than recalled. "I have already refunded you" cannot be un-said.

The guard evaluates exactly the units the chunker will speak, using the same tokenizer, by
import rather than by copy. If a guard and the thing it guards tokenize differently, that
difference is the bypass.

WHICH WAY THIS FAILS
--------------------
It fails closed, deliberately and everywhere. A refused sentence is silence where a
sentence would have been -- and the text is already on screen, so the buyer reads it and
is told it was not spoken (a ``speech_guard_refused`` degradation). A wrongly *spoken*
money claim cannot be recalled. So every ambiguous case refuses: an amount we cannot parse
exactly, a magnitude word that scales a figure past what we read, an amount written in
words, a sentence that names money and gives no checkable figure.

An earlier version of this module was built the other way round -- an allowlist of exact
verb forms -- and an adversarial review walked straight through it with
``Your payment was successful.``, ``Your money has been returned to your account.`` and
``Paisa wapas ho gaya hai.``. Vocabulary enumeration is not a safety property. What is
below refuses on the semantic frame instead: any claim that money moved, in any of the
three registers this platform speaks, whether or not a figure appears with it.

TWO KINDS OF MONEY, AND WHY ONLY ONE IS BANNED OUTRIGHT
-------------------------------------------------------
Specification 19.10 lists *transactional* money: the total being approved, a material
price or fee delta, a refund amount, a captured amount. Those are never model prose. A
product's list price quoted back during browsing -- "the 1 litre Amul Gold is 73 rupees"
-- is not on that list, and a guard that refuses it silently gags the shopping
conversation the voice surface exists to have.

So an amount is spoken only when it is **grounded**: present, to the paisa, in the set of
integer minor units a tool result of this turn actually returned. A hallucinated price is
refused; a quoted one is not. The danger was never that a price was spoken -- it was that
a price nothing computed was spoken.

Transaction outcomes stay refused **even when their amount is grounded**, because there is
no grounded version of "your refund is complete" that a model may author. The sentence is
the claim.

READING A FIGURE EXACTLY
------------------------
:func:`amounts_in` reads only currency-marked figures, through :class:`~decimal.Decimal`,
never a float. Two rules are load-bearing and both were bugs first:

* The grouped alternative requires **at least one** comma, and every figure is anchored
  with ``(?!\\d)``. With ``*`` and no anchor, ``\\d{1,3}`` matched greedily and succeeded,
  so the regex never tried the ungrouped alternative: ``₹1234`` was read as ``₹123`` and
  ``₹123456789`` as ``₹123``. The guard checked one number and the buyer heard another --
  in both directions, because a correctly grounded ``₹1299`` was also refused.
* Devanagari digits are folded to ASCII before parsing. Python's ``\\d`` matches ``७``,
  but ``Decimal("७३")`` raises, so an unfolded figure silently became no figure at all --
  and a sentence with no figure in it is a sentence with nothing to check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from .tokenizer import split_sentences

__all__ = [
    "IDENTIFIER_REQUEST",
    "MONEY_CONTEXT",
    "MONEY_FACT",
    "MONEY_MOVEMENT",
    "SPOKEN_IDENTIFIER",
    "TRANSACTION_OUTCOME",
    "GuardVerdict",
    "Refusal",
    "SpeechGuard",
    "amounts_in",
    "asks_for_identifier",
]

# --------------------------------------------------------------------------- outcomes


def _dev(*terms: str) -> str:
    r"""Alternation of Devanagari terms, each bounded by the Devanagari block.

    ``\b`` is the wrong boundary for Devanagari, and it is not a detail. Vowel signs and
    the virama are combining marks (Mn/Mc) and therefore not ``\w``, so a word boundary
    HOLDS in the middle of a word. ``बिल\b`` matched inside ``बिल्कुल`` -- "absolutely", the
    commonest affirmation in spoken Hindi and how a shopping reply opens; ``कर\b`` inside
    ``करें``; unanchored ``दाम`` inside ``दामाद``. Each refused an ordinary sentence as a
    money claim, and a refusal is silence the buyer is given no reason for.

    A Devanagari-block lookaround means here what ``\b`` means in English. It makes matching
    strictly narrower, so it cannot admit a claim that was refused on its own merits --
    only one that was never the word it appeared to be.
    """
    return "|".join(rf"(?<![\u0900-\u097f]){term}(?![\u0900-\u097f])" for term in terms)


#: Unambiguous claims that a transaction reached a state. Refused outright in model prose,
#: grounded or not: there is no version of "your refund is complete" a model may author.
#: English, Devanagari Hindi and romanised Hinglish, because the buyer speaks all three and
#: a guard that covers only one of them covers none.
TRANSACTION_OUTCOME: Final[re.Pattern[str]] = re.compile(
    # --- English: money moved, or an order/consent changed state
    r"\brefund\w*\b|\brefunded\b"
    r"|\bcaptur\w+\b|\bauthoris\w+\b|\bauthoriz\w+\b"
    r"|\bcharged?\b|\bdebit\w*\b|\bcredit(?:ed|ing)?\b|\bdeduct\w+\b"
    r"|\breimburs\w+\b|\brevers\w+\b|\bsettl\w+\b|\bchargeback\b"
    r"|\bpaid\b|\bpayment\w*\b|\bpay(?:ing|s)?\b|\bpayout\b"
    r"|\bapprov\w+\b|\breject\w+\b|\bconfirm\w+\b|\bcancel\w*\b"
    r"|\bmandate\w*\b|\brevok\w+\b|\bgrant(?:ed|s)?\b"
    r"|\breserv\w+\b|\bexpir\w+\b"
    r"|\btransaction\w*\b|\bcheckout\b|\breceipt\b|\binvoice\b"
    # --- Devanagari Hindi
    rf"|{_dev('भुगतान', 'रिफ़ंड', 'रिफंड', 'वापिस', 'स्वीकार', 'मंज़ूर', 'मंजूर')}"
    # Stems that inflect keep their `\w*` tail and take the leading bound only.
    r"|(?<![ऀ-ॿ])(?:वापस|स्वीकृ)\w*"
    # `आदेश`/`ऑर्डर` are NOT here, deliberately. English "order" is a _MONEY_NOUN, refused
    # only when a movement verb stands beside it, so "your order has two packets" is spoken
    # while "your order is complete" is not. Both Hindi words were outright outcomes, which
    # held a Hindi buyer to a stricter rule than an English one for the same sentence and
    # gagged ordinary basket talk. They sit in _MONEY_NOUN below, where MONEY_MOVEMENT
    # still catches "आपका ऑर्डर पूरा हो गया". This is parity, not relaxation: the claim that
    # money moved is what is refused, in every register.
    rf"|{_dev('रद्द', 'निरस्त', 'जमा', 'क्रेडिट', 'डेबिट', 'अदा', 'पैसा', 'पैसे', 'रसीद', 'बिल')}"
    r"|(?<![ऀ-ॿ])कट\s*गय|(?<![ऀ-ॿ])(?:चुका|लौटा)\w*"
    # --- Hinglish in Latin script: the register the buyer actually speaks
    r"|\bbhugtan\b|\bwapas\w*\b|\bwapis\w*\b|\bwaapas\w*\b|\bwapsi\b"
    r"|\bpaisa\b|\bpaise\b|\bpaisay\b|\brupay\w*\b|\brupya\w*\b"
    r"|\bkat\s*gay\w*\b|\bjama\b|\bchuka\w*\b|\blauta\w*\b|\bmanzoor\b|\bmanjoor\b"
    r"|\bcancel\s*(?:ho|kar)\w*\b|\bho\s*gaya\s*hai\b|\brasid\b",
    re.IGNORECASE,
)

#: Nouns that name money or the thing money moves through.
_MONEY_NOUN: Final[str] = (
    r"money|amount|funds?|balance|account|card|wallet|order|basket|cart"
    r"|पैसा|पैसे|रकम|खाता|कार्ड|ऑर्डर|आदेश"
)
#: Verbs that mean it moved. Individually ambiguous ("returned", "sent", "went through"),
#: which is why they are only refused when a money noun stands beside them.
_MOVED_VERB: Final[str] = (
    r"return\w*|sent|send|back|through|process\w*|complet\w*|success\w*|fail\w*"
    r"|pending|done|placed|received|withdraw\w*|deposit\w*|transferr?\w*"
    r"|लौट\w*|भेज\w*|पूरा|सफल|विफल|लंबित|मिल\s*गय\w*"
)

#: A money noun and a movement verb in the same sentence. "Your money has been returned to
#: your account" trips no outcome word on its own; this is what catches it.
MONEY_MOVEMENT: Final[re.Pattern[str]] = re.compile(
    rf"(?:{_MONEY_NOUN})[\s\S]{{0,80}}?(?:{_MOVED_VERB})"
    rf"|(?:{_MOVED_VERB})[\s\S]{{0,80}}?(?:{_MONEY_NOUN})",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------- amounts

#: Anything that reads as money. A hit only means "this sentence talks about money", which
#: sends it to the grounding check rather than refusing it.
MONEY_FACT: Final[re.Pattern[str]] = re.compile(
    r"₹|\bINR\b|\bRs\.?\s*[\d०-९]|\brupees?\b|\bpaise?\b"
    r"|[\d०-९]{1,3}(?:,[\d०-९]{2,3})+(?:\.[\d०-९]+)?"
    r"|\b[\d०-९]+\.[\d०-९]{2}\b"
    r"|रुपये|रुपए|रुपया",
    re.IGNORECASE,
)

#: Words that name a money quantity without naming a currency. Paired with a number, these
#: make a numeric money claim: "Your total is 3950" carries no currency mark at all.
MONEY_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"\btotals?\b|\bsub-?totals?\b|\bbills?\b|\bamounts?\b|\bprices?\b|\bcosts?\b"
    r"|\bcharges?\b|\bfees?\b|\bdiscounts?\b|\btax(?:es)?\b|\bsum\b|\bdue\b|\bpayable\b"
    # Every Devanagari term is bounded -- see `_dev`. Unbounded, `कुल` matched inside
    # `बिलकुल`, `दाम` inside `दामाद` and `कर` inside `करें`, and each turned an ordinary
    # sentence carrying a number word into an unverifiable "amount in words".
    # Bounded, they still match the nouns they are for: "सेवा कर ₹50", "कुल ₹119".
    rf"|{_dev('बिल', 'कुल', 'दाम', 'कीमत', 'शुल्क', 'छूट', 'कर')}",
    re.IGNORECASE,
)

#: A magnitude word scales a figure past what the digits say, so the number checked and the
#: number heard differ: "₹5 lakh" reads as 5. Unverifiable, therefore refused.
_MAGNITUDE: Final[re.Pattern[str]] = re.compile(
    r"\blakhs?\b|\blacs?\b|\bcrores?\b|\bthousands?\b|\bhazaar\b|\bhazar\b"
    r"|\bmillions?\b|\bbillions?\b|\bdozens?\b"
    r"|लाख|करोड़|करोड|हज़ार|हजार",
    re.IGNORECASE,
)

#: Numbers written as words. "one" and "a" are excluded on purpose: "which one" and
#: "a litre" are not numeric claims, and refusing them would gag ordinary conversation.
_NUMBER_WORD: Final[re.Pattern[str]] = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen"
    r"|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty"
    r"|seventy|eighty|ninety|hundred|thousand)\b"
    r"|\b(?:do|teen|char|paanch|panch|chhe|chah|saat|aath|nau|das|bees|tees|chalis"
    r"|pachas|sau|hazaar|hazar)\b"
    r"|दो|तीन|चार|पाँच|पांच|छह|सात|आठ|नौ|दस|बीस|तीस|चालीस|पचास|सौ",
    re.IGNORECASE,
)

#: A number word used AS an amount: adjacent to a currency term, in either order.
#:
#: Adjacency is what makes this usable. A quantity word is not an amount word, and the
#: first version of this rule refused any sentence that held both a grounded figure and a
#: number word -- which gagged the entire product listing, because RazorAI quotes the
#: buyer's own request back ("I found 5 products for "Two liters of milk, please."") and
#: "Two" is a number word. Found by speaking to the running gateway, not in a test.
_WORDS_AS_MONEY: Final[re.Pattern[str]] = re.compile(
    rf"(?:{_NUMBER_WORD.pattern})(?:[\s-]+(?:and|{_NUMBER_WORD.pattern}))*"
    rf"[\s-]*(?:rupees?\b|paise?\b|रुपये|रुपए|रुपया|₹|\bINR\b)"
    rf"|(?:₹|\bRs\.?|\bINR)\s*(?:{_NUMBER_WORD.pattern})",
    re.IGNORECASE,
)

#: A bare run of digits, with no currency mark. Only a claim when money context sits with it.
_BARE_NUMBER: Final[re.Pattern[str]] = re.compile(r"[\d०-९]")

#: Digits, ASCII or Devanagari. ``\d`` matches ``७`` but ``Decimal("७")`` raises, so an
#: unfolded figure parsed to nothing -- and a sentence with no figure has nothing to check.
_DIGITS: Final[str] = r"[\d०-९]"
_DEVANAGARI_TO_ASCII: Final[dict[int, str]] = {0x0966 + offset: str(offset) for offset in range(10)}

#: A figure. The grouped alternative demands at least one comma, so an ungrouped number
#: falls through to the second; ``(?!\d)`` forbids a partial match. Both were bugs.
_FIGURE: Final[str] = (
    rf"-?{_DIGITS}{{1,3}}(?:,{_DIGITS}{{2,3}})+(?:\.{_DIGITS}{{1,2}})?(?!{_DIGITS})"
    rf"|-?{_DIGITS}+(?:\.{_DIGITS}{{1,2}})?(?!{_DIGITS})"
)

#: A sign may sit OUTSIDE the currency mark: "-₹73" is minus seventy-three rupees, and
#: reading it as +73 matched a grounded credit against a debit. Any of the three dashes a
#: model writes counts.
_SIGN: Final[str] = r"[-\u2212\u2013\u2014]"

_RUPEES_LEADING: Final[re.Pattern[str]] = re.compile(
    rf"(?P<sign>{_SIGN})?\s*(?:₹|\bRs\.?|\bINR)\s*(?P<num>{_FIGURE})", re.IGNORECASE
)
#: The marker may also trail. RazorAI renders a product price as ``79.00 INR`` -- found by
#: speaking to the running system, not by reading the renderer.
_RUPEES_TRAILING: Final[re.Pattern[str]] = re.compile(
    rf"({_FIGURE})\s*(?:INR\b|Rs\.?\B|rupees?|रुपये|रुपए|रुपया)", re.IGNORECASE
)
_PAISE_TRAILING: Final[re.Pattern[str]] = re.compile(rf"({_FIGURE})\s*(?:paise?|पैसे)", re.IGNORECASE)

#: Minor units per major unit for INR. Voice is INR-only in P0; a second currency needs a
#: second decision about how it is spoken, not just a different exponent.
_MINOR_PER_MAJOR: Final[int] = 100


# ------------------------------------------------------------------------ identifiers

#: Anything shaped like an identifier this platform issues or receives.
#:
#: An identifier is written to be read, not heard. `RS-260908-K7M4QX2` takes about nine
#: seconds to speak, arrives as noise, and is gone -- a buyer listening cannot pause it,
#: cannot scroll back, and has nowhere to put it. Reading one aloud unasked is not extra
#: helpfulness; it is filling the one channel the buyer has with the one form of the fact
#: they cannot use, in a medium where every second spent is a second they waited.
#:
#: The screen already has it. Text exists before speech on every turn (19.1), so the
#: reference the assistant is not saying is visible while it talks.
#:
#: Four shapes, all of them ours or the provider's:
#:
#: * the order reference, `RS-YYMMDD-XXXXXXX`, which is the one a person could plausibly
#:   say back -- and is still not said until it is wanted;
#: * a UUID, which is every internal id;
#: * a provider or kernel handle -- `pay_`, `order_`, `rfnd_`, `rcpt_`, `grant_`;
#: * a bare run of sixteen or more characters mixing letters and digits, which is what a
#:   hash or an idempotency key looks like once the markup is stripped for speech.
SPOKEN_IDENTIFIER: Final[re.Pattern[str]] = re.compile(
    r"\bRS-\d{6}-[0-9A-HJKMNP-TV-Z]{4,}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\b(?:pay|order|rfnd|rcpt|grant|plink|cust)_[A-Za-z0-9]{6,}\b"
    r"|\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}\b",
    re.IGNORECASE,
)

#: The buyer asking for one, in the three registers they actually speak.
#:
#: Deliberately generous. The cost of hearing an identifier the buyer half-asked for is a
#: few seconds; the cost of refusing one they asked for outright is an assistant that
#: appears not to know its own order numbers, which is worse than either. The asymmetry
#: decides the width, and it is the buyer's own turn that opens the door -- never the
#: model's judgement about whether now would be a good time.
IDENTIFIER_REQUEST: Final[re.Pattern[str]] = re.compile(
    # --- English
    r"\border\s*(?:number|no\.?|id)\b|\breference\b|\bref\s*(?:number|no\.?)?\b"
    r"|\btracking\s*(?:number|id)\b|\bwhich\s+order\b|\bwhat(?:'s| is)\s+my\s+order\b"
    r"|\bpayment\s*id\b|\btransaction\s*id\b|\bread\s+(?:it|that)\s+(?:out|back)\b"
    r"|\bspell\b|\bsay\s+(?:it|that)\s+again\b"
    # --- Devanagari Hindi
    rf"|{_dev('नंबर', 'संदर्भ', 'आईडी', 'कौनसा', 'कौन')}"
    # --- Hinglish in Latin script
    r"|\bnumber\s*(?:kya|batao?|bata)\b|\bkya\s*(?:hai\s*)?number\b"
    r"|\bid\s*(?:kya|batao?|bata)\b|\bkaun\s*sa\s*order\b|\bkaunsa\b"
    r"|\breference\s*(?:kya|batao?|bata)\b",
    re.IGNORECASE,
)


def asks_for_identifier(text: str) -> bool:
    """Did the buyer's own turn ask to be told an identifier?

    The buyer's words and nothing else. Not the model's intent, not the tool that ran,
    not whether an id happens to be on the screen -- because each of those is a way for
    the assistant to decide on the buyer's behalf that now is a good moment to recite a
    UUID, and the whole rule is that it is not the assistant's call.
    """
    return bool(text) and IDENTIFIER_REQUEST.search(text) is not None


def _scaled(raw: str, factor: int) -> int | None:
    """``raw`` scaled to integer minor units, or ``None`` if it is not a whole number.

    Read through :class:`~decimal.Decimal`, never a float: parsing an amount through a
    float is how a paisa goes missing between the screen and the speaker.
    """
    folded = raw.translate(_DEVANAGARI_TO_ASCII).replace(",", "")
    try:
        value = Decimal(folded) * factor
    except InvalidOperation:  # pragma: no cover - the pattern admits only decimals
        return None
    return int(value) if value == value.to_integral_value() else None


def amounts_in(sentence: str) -> frozenset[int]:
    """Every currency-marked figure in ``sentence`` as integer minor units, read exactly.

    A figure with no currency marker beside it is not returned. Such a sentence still
    reaches the grounding check through :data:`MONEY_FACT` or :data:`MONEY_CONTEXT`, and a
    sentence that names money while giving no checkable figure is refused -- so the
    untagged case fails closed rather than passing unexamined.
    """
    found: set[int] = set()
    for pattern, factor in (
        (_RUPEES_LEADING, _MINOR_PER_MAJOR),
        (_RUPEES_TRAILING, _MINOR_PER_MAJOR),
        (_PAISE_TRAILING, 1),
    ):
        for match in pattern.finditer(sentence):
            groups = match.groupdict()
            minor = _scaled(groups.get("num") or match.group(1), factor)
            if minor is None:
                continue
            if groups.get("sign"):
                minor = -abs(minor)
            found.add(minor)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Refusal:
    sentence: str
    reason: str


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """What may be spoken, in chunker order, and what was refused with why."""

    allowed: tuple[str, ...]
    refused: tuple[Refusal, ...]

    @property
    def refused_any(self) -> bool:
        return bool(self.refused)


class SpeechGuard:
    """Sentence-level guard over model-authored text."""

    #: Identity with the chunker's tokenizer is asserted by test, not assumed.
    tokenizer = staticmethod(split_sentences)

    def check(
        self,
        text: str,
        *,
        deterministic: bool,
        grounded_amounts_minor: frozenset[int] = frozenset(),
        identifiers_allowed: bool = False,
    ) -> GuardVerdict:
        """Template speech passes whole; model text is checked sentence by sentence."""
        sentences = split_sentences(text)
        if deterministic:
            # Rendered from server-confirmed fields by templates.py, which no model can
            # reach. There is nothing here for a guard to second-guess.
            return GuardVerdict(allowed=tuple(sentences), refused=())
        allowed: list[str] = []
        refused: list[Refusal] = []
        for sentence in sentences:
            reason = self.reason_to_refuse(
                sentence, grounded_amounts_minor, identifiers_allowed=identifiers_allowed
            )
            if reason is None:
                allowed.append(sentence)
            else:
                refused.append(Refusal(sentence=sentence, reason=reason))
        return GuardVerdict(allowed=tuple(allowed), refused=tuple(refused))

    @staticmethod
    def reason_to_refuse(
        sentence: str,
        grounded_amounts_minor: frozenset[int] = frozenset(),
        *,
        identifiers_allowed: bool = False,
    ) -> str | None:
        """Why this sentence may not be spoken by a model, or ``None`` if it may."""
        if not identifiers_allowed and SPOKEN_IDENTIFIER.search(sentence):
            # Unlike every other refusal here, this one is not about truth. The
            # identifier is correct; it is simply unusable as sound, and it is on the
            # screen already. The buyer's own turn is what opens this, and until it does
            # the sentence carrying it is not spoken.
            return "identifier_not_requested"
        if TRANSACTION_OUTCOME.search(sentence):
            return "transaction_outcome_outside_template"
        if MONEY_MOVEMENT.search(sentence):
            return "money_movement_outside_template"

        marked = MONEY_FACT.search(sentence) is not None
        context = MONEY_CONTEXT.search(sentence) is not None
        has_digits = _BARE_NUMBER.search(sentence) is not None
        has_words = _NUMBER_WORD.search(sentence) is not None
        # A numeric money claim is either currency-marked, or a money word standing beside
        # a number. "The price is right" claims nothing; "Your total is 3950" does.
        if not (marked or (context and (has_digits or has_words))):
            return None

        if _MAGNITUDE.search(sentence):
            # "₹5 lakh" reads as 5: the number checked is not the number heard.
            return "unverifiable_magnitude"
        if _WORDS_AS_MONEY.search(sentence):
            # "seventy-three rupees" cannot be checked against a set of integers.
            return "amount_in_words"
        spoken = amounts_in(sentence)
        if not spoken:
            if has_words:
                return "amount_in_words"
            # Names money, gives no figure this guard can read against the tool results.
            return "money_fact_without_amount"
        if spoken - grounded_amounts_minor:
            return "ungrounded_amount"
        return None

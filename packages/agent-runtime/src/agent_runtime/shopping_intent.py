"""Closed conversational references; never infer a product from an ambiguous number."""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ProductReference:
    index: int | None
    quantity: int
    mode: str = "add"
    corrects_previous: bool = False


_ORDINALS = {
    "first": 0,
    "1st": 0,
    "pehla": 0,
    "पहला": 0,
    "पहले": 0,
    "second": 1,
    "2nd": 1,
    "dusra": 1,
    "doosra": 1,
    "दूसरा": 1,
    "दूसरे": 1,
    "third": 2,
    "3rd": 2,
    "teesra": 2,
    "तीसरा": 2,
    "तीसरे": 2,
    "fourth": 3,
    "4th": 3,
    "chautha": 3,
    "चौथा": 3,
    "fifth": 4,
    "5th": 4,
    "paanchva": 4,
    "पाँचवाँ": 4,
}
_QUANTITIES = {
    "one": 1,
    "ek": 1,
    "एक": 1,
    "two": 2,
    "do": 2,
    "दो": 2,
    "three": 3,
    "teen": 3,
    "तीन": 3,
    "four": 4,
    "chaar": 4,
    "चार": 4,
    "five": 5,
    "paanch": 5,
    "पाँच": 5,
    "six": 6,
    "chhe": 6,
    "छह": 6,
    "seven": 7,
    "saat": 7,
    "सात": 7,
    "eight": 8,
    "aath": 8,
    "आठ": 8,
    "nine": 9,
    "nau": 9,
    "नौ": 9,
    "ten": 10,
    "das": 10,
    "दस": 10,
}
_REF = "(?:" + "|".join(_ORDINALS) + r")(?:\s+(?:one|item|product|wala|वाला))?"
_REF = rf"(?:{_REF}|it|this|that|isko|ise|इसे|इसको)"
_QTY = r"(?:[1-9]|10|" + "|".join(_QUANTITIES) + ")"


def product_reference(message: str) -> ProductReference | None:
    value = normalize(message)
    correction = re.fullmatch(
        rf"(?P<old>{_QTY}) (?:nahi|nahin|नहीं|नही),? (?P<q>{_QTY})(?: (?:chahiye|चाहिए))?"
        rf"|(?:no,? )?not (?P<old_en>{_QTY}),? (?:make it |set it to )?(?P<q_en>{_QTY})",
        value,
    )
    if correction:
        return ProductReference(
            None, parse_count(correction["q"] or correction["q_en"]), "set", True
        )
    absolute = re.fullmatch(
        rf"(?:set|make) (?:the )?(?P<r>{_REF})(?: quantity)? (?:to )?(?P<q>{_QTY})"
        rf"|(?P<rhi>{_REF}) (?:ki |की )?quantity (?P<qhi>{_QTY}) (?:kar do|karo|कर दो)",
        value,
    )
    removal = re.fullmatch(
        rf"remove (?:the )?(?P<r>{_REF})(?: from (?:my |the )?cart)?"
        rf"|(?P<rhi>{_REF})(?: cart se| कार्ट से)? (?:hatao|hata do|हटाओ|हटा दो)",
        value,
    )
    if absolute or removal:
        match = absolute or removal
        assert match is not None
        ref = match["r"] or match["rhi"]
        raw = (match["q"] or match["qhi"]) if absolute else "0"
        quantity = int(raw) if raw.isdecimal() else _QUANTITIES[raw]
        return ProductReference(_ORDINALS.get(ref.split()[0]), quantity, "set")
    patterns = (
        rf"(?:please )?add (?:(?P<q>{_QTY}) (?:of )?)?(?:the )?(?P<r>{_REF})"
        r"(?: to (?:my |the )?cart)?(?: please)?",
        rf"(?P<r>{_REF})(?:\s+(?:ki|ke|के|की))?(?:\s+(?P<q>{_QTY})"
        r"(?:\s+(?:quantity|units|pieces|packs?|packets?|पैक|पैकेट))?)?"
        r"(?:\s+(?:cart mein|cart me|कार्ट में))?\s+"
        r"(?:add karo|add kar do|daal do|dal do|jod do|jodo|jod kar do|जोड़ दो|जोड़ो|डाल दो|डालो)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            raw = match["q"] or "1"
            quantity = int(raw) if raw.isdecimal() else _QUANTITIES[raw]
            return ProductReference(_ORDINALS.get(match["r"].split()[0]), quantity)
    return None


@dataclass(frozen=True)
class NamedCartIntent:
    query: str
    quantity: int
    mode: str = "add"


def normalize(message: str) -> str:
    text = unicodedata.normalize("NFKC", message).casefold()
    text = "".join(str(unicodedata.digit(c)) if c.isdecimal() else c for c in text)
    return re.sub(r"\s+", " ", text).strip(" .!।")


def parse_count(raw: str) -> int:
    return int(raw) if raw.isdecimal() else _QUANTITIES[raw]


def named_cart_intent(message: str) -> NamedCartIntent | None:
    """Parse only explicit single-product commands; queries remain untrusted text.

    The service must resolve the entire query against fresh catalogue facts. A number
    belongs to quantity only in the command's count position, never inside a pack name.
    """
    value = normalize(message)
    if product_reference(value) is not None or "?" in value:
        return None
    units = r"(?:packs?|packets?|pieces?|units?|पैक|पैकेट|नग)"
    cart = r"(?:cart (?:mein|me)|कार्ट में)"
    verb = r"(?:add (?:kar do|karo)|daal do|dal do|jod do|jodo|jod kar do|जोड़ दो|जोड़ो|डाल दो|डालो)"
    patterns = (
        rf"(?:please )?add (?P<q>{_QTY})(?: {units})? (?:of )?(?P<name>.+?)"
        r"(?: to (?:my |the )?cart)?",
        rf"(?P<q>{_QTY})(?: {units})? (?P<name>.+?)(?: {cart})? {verb}",
        rf"(?P<name>.+?) (?P<q>{_QTY})(?: {units})?(?: {cart})? {verb}",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return NamedCartIntent(match["name"], parse_count(match["q"]))
    removal = re.fullmatch(
        r"remove (?P<name>.+?)(?: from (?:my |the )?cart)?"
        r"|(?P<hi>.+?)(?: cart se| कार्ट से)? (?:hatao|hata do|हटाओ|हटा दो)",
        value,
    )
    if removal:
        return NamedCartIntent(removal["name"] or removal["hi"], 0, "set")
    absolute = re.fullmatch(
        rf"(?:set|make) (?P<name>.+?)(?: quantity)? (?:to )(?P<q>{_QTY})"
        rf"|(?P<hi>.+?) (?:ki |की )?(?:quantity|मात्रा) (?P<n>{_QTY}) (?:kar do|karo|कर दो)",
        value,
    )
    if absolute:
        return NamedCartIntent(
            absolute["name"] or absolute["hi"], parse_count(absolute["q"] or absolute["n"]), "set"
        )
    return None

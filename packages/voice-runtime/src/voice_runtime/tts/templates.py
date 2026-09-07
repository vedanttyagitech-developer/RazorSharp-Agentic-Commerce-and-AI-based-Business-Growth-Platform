"""Deterministic transactional speech (19.10).

The model does not author speech for approvals, amounts, deltas, reservation expiry,
payment outcomes, cancellation effects, refunds or delegated authority. Those sentences are
rendered here from versioned locale templates filled with server-confirmed structured
fields taken from a ``KernelDecision``. Nothing in this module imports a model SDK, and
``tests/test_voice_templates.py`` asserts that it never can.

Every rendered utterance records its template ID, locale, template version and the exact
verified fields, so the audit trail can show what was said and why (19.10). Amounts are
spoken as words and digits with Indian grouping and paise, so the spoken figure and the
figure on the approval card are the same fact twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

from commerce_domain import Money, exponent_for
from transaction_kernel.contracts import Delta, KernelDecision
from transaction_kernel.recovery import RecoveryCode


class Locale(StrEnum):
    EN_IN = "en-IN"
    HI_IN = "hi-IN"


#: Bump when any template body changes; the version is recorded with every utterance.
TEMPLATE_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class RenderedSpeech:
    """Server-authored speech plus the audit facts that produced it."""

    text: str
    template_id: str
    template_version: int
    locale: Locale
    fields: Mapping[str, str]
    deterministic: Literal[True] = True


# ---- numbers -------------------------------------------------------------------------

_EN_ONES: Final[tuple[str, ...]] = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_EN_TENS: Final[tuple[str, ...]] = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)

# Hindi numerals are irregular below one hundred; each has its own word.
_HI_0_99: Final[tuple[str, ...]] = (
    "शून्य", "एक", "दो", "तीन", "चार",
    "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह",
    "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस",
    "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस",
    "पच्चीस", "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस",
    "तीस", "इकतीस", "बत्तीस", "तैंतीस", "चौंतीस",
    "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
    "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चौवालीस",
    "पैंतालीस", "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास",
    "पचास", "इक्यावन", "बावन", "तिरपन", "चौवन",
    "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ",
    "साठ", "इकसठ", "बासठ", "तिरसठ", "चौंसठ",
    "पैंसठ", "छियासठ", "सड़सठ", "अड़सठ", "उनहत्तर",
    "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर",
    "पचहत्तर", "छिहत्तर", "सतहत्तर", "अठहत्तर", "उनासी",
    "अस्सी", "इक्यासी", "बयासी", "तिरासी", "चौरासी",
    "पचासी", "छियासी", "सतासी", "अठासी", "नवासी",
    "नब्बे", "इक्यानवे", "बानवे", "तिरानवे", "चौरानवे",
    "पचानवे", "छियानवे", "सत्तानवे", "अट्ठानवे", "निन्यानवे",
)  # fmt: skip

_CRORE: Final[int] = 10_000_000
_LAKH: Final[int] = 100_000


def _en_below_thousand(n: int) -> str:
    parts: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(f"{_EN_ONES[hundreds]} hundred")
    if rest < 20:
        if rest or not parts:
            parts.append(_EN_ONES[rest])
    else:
        tens, ones = divmod(rest, 10)
        parts.append(_EN_TENS[tens] + (f"-{_EN_ONES[ones]}" if ones else ""))
    return " ".join(parts)


def int_to_words_en(n: int) -> str:
    """Indian-English cardinal: crore, lakh, thousand, hundred."""
    if n < 0:
        return f"minus {int_to_words_en(-n)}"
    if n == 0:
        return "zero"
    parts: list[str] = []
    crore, n = divmod(n, _CRORE)
    lakh, n = divmod(n, _LAKH)
    thousand, rest = divmod(n, 1000)
    if crore:
        parts.append(f"{int_to_words_en(crore)} crore")
    if lakh:
        parts.append(f"{_en_below_thousand(lakh)} lakh")
    if thousand:
        parts.append(f"{_en_below_thousand(thousand)} thousand")
    if rest:
        parts.append(_en_below_thousand(rest))
    return " ".join(parts)


def _hi_below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts: list[str] = []
    if hundreds:
        parts.append(f"{_HI_0_99[hundreds]} सौ")
    if rest or not parts:
        parts.append(_HI_0_99[rest])
    return " ".join(parts)


def int_to_words_hi(n: int) -> str:
    """Hindi cardinal: करोड़, लाख, हज़ार, सौ."""
    if n < 0:
        return f"ऋण {int_to_words_hi(-n)}"
    if n == 0:
        return _HI_0_99[0]
    parts: list[str] = []
    crore, n = divmod(n, _CRORE)
    lakh, n = divmod(n, _LAKH)
    thousand, rest = divmod(n, 1000)
    if crore:
        parts.append(f"{int_to_words_hi(crore)} करोड़")
    if lakh:
        parts.append(f"{_hi_below_thousand(lakh)} लाख")
    if thousand:
        parts.append(f"{_hi_below_thousand(thousand)} हज़ार")
    if rest:
        parts.append(_hi_below_thousand(rest))
    return " ".join(parts)


def _group_indian(whole: str) -> str:
    """``395000`` -> ``3,95,000``: last three digits, then groups of two."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return ",".join(groups) + "," + tail


def _group_western(whole: str) -> str:
    return f"{int(whole):,}"


def format_money_digits(money: Money) -> str:
    """Locale-correct digits: ``₹3,95,000.50`` for INR, ``USD 1,234.56`` otherwise."""
    exp = exponent_for(money.currency)
    sign = "-" if money.minor < 0 else ""
    whole, frac = divmod(abs(money.minor), 10**exp)
    fraction = f".{frac:0{exp}d}" if exp else ""
    if money.currency == "INR":
        return f"{sign}₹{_group_indian(str(whole))}{fraction}"
    return f"{sign}{money.currency} {_group_western(str(whole))}{fraction}"


def money_to_words(money: Money, locale: Locale) -> str:
    """Amount in words with paise, in the requested locale."""
    exp = exponent_for(money.currency)
    negative = money.minor < 0
    whole, frac = divmod(abs(money.minor), 10**exp)
    if locale is Locale.HI_IN:
        unit = "रुपये" if money.currency == "INR" else money.currency
        text = f"{int_to_words_hi(whole)} {unit}"
        if frac:
            text += f" {int_to_words_hi(frac)} पैसे"
        return f"ऋण {text}" if negative else text
    if money.currency == "INR":
        major, minor = ("rupee" if whole == 1 else "rupees"), "paise"
    else:
        major, minor = money.currency, ("cents" if exp == 2 else "minor units")
    text = f"{int_to_words_en(whole)} {major}"
    if frac:
        text += f" and {int_to_words_en(frac)} {minor}"
    return f"minus {text}" if negative else text


def spoken_amount(money: Money, locale: Locale) -> str:
    """Digits and words together: the two forms of one server-confirmed fact."""
    return f"{format_money_digits(money)}, {money_to_words(money, locale)}"


# ---- templates -----------------------------------------------------------------------

_ABSENT_AMOUNT: Final[dict[Locale, str]] = {
    Locale.EN_IN: "the approved amount",
    Locale.HI_IN: "स्वीकृत राशि",
}
_ABSENT_PREVIOUS: Final[dict[Locale, str]] = {
    Locale.EN_IN: "the earlier amount",
    Locale.HI_IN: "पहले की राशि",
}
_ABSENT_VERSION: Final[dict[Locale, str]] = {Locale.EN_IN: "unknown", Locale.HI_IN: "अज्ञात"}

# Keyed by RecoveryCode: every decision type has a sentence in both locales.
_BY_CODE: Final[dict[Locale, dict[RecoveryCode, str]]] = {
    Locale.EN_IN: {
        RecoveryCode.OK: (
            "Version {version} is admitted for {amount_phrase}. Razorpay will now "
            "authorise the payment. I will report the outcome exactly as Razorpay "
            "confirms it."
        ),
        RecoveryCode.DUPLICATE_OPERATION: (
            "This payment is already in progress for version {version}. Nothing was "
            "submitted twice."
        ),
        RecoveryCode.CONCURRENT_OPERATION: (
            "Another attempt on this checkout is already live. Nothing was submitted "
            "twice. Check the attempt shown on screen."
        ),
        RecoveryCode.STALE_CHECKOUT: (
            "The checkout you approved, version {version}, is no longer current. "
            "{deltas}Review the current version on screen before approving again."
        ),
        RecoveryCode.REAPPROVAL_REQUIRED: (
            "The earlier approval for {previous_amount_phrase} is no longer valid. The "
            "new total is {amount_phrase}. {deltas}Version {next_version} is ready. "
            "Review the changes on screen before approving."
        ),
        RecoveryCode.RESERVATION_EXPIRED: (
            "The reservation for version {version} has expired. No payment was made. "
            "Review availability on screen to continue."
        ),
        RecoveryCode.SOLD_OUT: (
            "Everything in version {version} sold out before payment. No payment was made "
            "and the items have been released. Start a new order on screen to continue."
        ),
        RecoveryCode.AUTHORITY_REVOKED: (
            "The delegated authority for this action has been revoked. No payment was made."
        ),
        RecoveryCode.AUTHORITY_INSUFFICIENT: (
            "This action needs an approval on the trusted surface. No payment was made. {deltas}"
        ),
        RecoveryCode.PAYMENT_FAILED: (
            "The payment for {amount_phrase} failed. No money was captured. You can retry "
            "from the screen."
        ),
        RecoveryCode.PAYMENT_PENDING: (
            "Payment is pending. I will not retry until Razorpay confirms the outcome."
        ),
        RecoveryCode.PAYMENT_UNKNOWN: (
            "The payment outcome is not yet known. I will not retry. Reconciliation is in "
            "progress and I will report exactly what Razorpay confirms."
        ),
        RecoveryCode.STALE_CAPTURE: (
            "A capture arrived for a checkout version that is no longer valid. It is being "
            "handled. No order will be fulfilled against it."
        ),
        RecoveryCode.REFUND_ALLOWED: (
            "A refund of {amount_phrase} has been requested. It is not yet complete."
        ),
        RecoveryCode.REFUND_REVIEW_REQUIRED: (
            "The refund of {amount_phrase} needs review before it can proceed. Nothing has "
            "been refunded yet."
        ),
        RecoveryCode.RESOLUTION_PLAN_ISSUED: (
            "A resolution plan has been issued. Review it on screen. Nothing changes until "
            "you accept it."
        ),
        RecoveryCode.RESOLUTION_PLAN_EXPIRED: (
            "The resolution plan has expired. No change was made."
        ),
        RecoveryCode.RECONCILIATION_IN_PROGRESS: (
            "Reconciliation with Razorpay is in progress. I will not retry until it completes."
        ),
        RecoveryCode.POLICY_EXCEPTION: (
            "Merchant policy does not allow this action. No change was made."
        ),
        RecoveryCode.HUMAN_REVIEW_REQUIRED: (
            "This needs human review before anything happens. No change was made."
        ),
        RecoveryCode.CONNECTOR_UNAVAILABLE: (
            "The merchant's system is not responding. Nothing was submitted and no payment "
            "was made. I will not read out a price I cannot confirm."
        ),
        RecoveryCode.SAFE_MODE_ACTIVE: (
            "Safe mode is active. Money actions are paused. No change was made."
        ),
    },
    Locale.HI_IN: {
        RecoveryCode.OK: (
            "संस्करण {version} के लिए {amount_phrase} का भुगतान स्वीकार किया गया है। अब "
            "Razorpay भुगतान को अधिकृत करेगा। परिणाम मैं ठीक वैसे ही बताऊँगी जैसे Razorpay "
            "पुष्टि करेगा।"
        ),
        RecoveryCode.DUPLICATE_OPERATION: (
            "संस्करण {version} के लिए यह भुगतान पहले से चल रहा है। कुछ भी दो बार नहीं भेजा गया।"
        ),
        RecoveryCode.CONCURRENT_OPERATION: (
            "इस चेकआउट पर एक और प्रयास पहले से चल रहा है। कुछ भी दो बार नहीं भेजा गया। स्क्रीन "
            "पर दिखाया गया प्रयास देखें।"
        ),
        RecoveryCode.STALE_CHECKOUT: (
            "आपने जो चेकआउट स्वीकृत किया था, संस्करण {version}, अब मान्य नहीं है। {deltas}दोबारा "
            "स्वीकृति देने से पहले स्क्रीन पर वर्तमान संस्करण देखें।"
        ),
        RecoveryCode.REAPPROVAL_REQUIRED: (
            "{previous_amount_phrase} की पहले की स्वीकृति अब मान्य नहीं है। नया कुल "
            "{amount_phrase} है। {deltas}संस्करण {next_version} तैयार है। स्वीकृति देने से पहले "
            "स्क्रीन पर बदलाव देखें।"
        ),
        RecoveryCode.RESERVATION_EXPIRED: (
            "संस्करण {version} का आरक्षण समाप्त हो गया है। कोई भुगतान नहीं हुआ। जारी रखने के लिए "
            "स्क्रीन पर उपलब्धता देखें।"
        ),
        RecoveryCode.SOLD_OUT: (
            "संस्करण {version} की सारी वस्तुएँ भुगतान से पहले समाप्त हो गईं। कोई भुगतान नहीं हुआ और "
            "वस्तुएँ छोड़ दी गई हैं। जारी रखने के लिए स्क्रीन पर नया ऑर्डर शुरू करें।"
        ),
        RecoveryCode.AUTHORITY_REVOKED: (
            "इस कार्रवाई के लिए दिया गया अधिकार रद्द कर दिया गया है। कोई भुगतान नहीं हुआ।"
        ),
        RecoveryCode.AUTHORITY_INSUFFICIENT: (
            "इस कार्रवाई के लिए विश्वसनीय स्क्रीन पर स्वीकृति चाहिए। कोई भुगतान नहीं हुआ। {deltas}"
        ),
        RecoveryCode.PAYMENT_FAILED: (
            "{amount_phrase} का भुगतान विफल रहा। कोई राशि नहीं ली गई। आप स्क्रीन से दोबारा प्रयास "
            "कर सकते हैं।"
        ),
        RecoveryCode.PAYMENT_PENDING: (
            "भुगतान लंबित है। जब तक Razorpay परिणाम की पुष्टि नहीं करता, मैं दोबारा प्रयास नहीं करूँगी।"
        ),
        RecoveryCode.PAYMENT_UNKNOWN: (
            "भुगतान का परिणाम अभी ज्ञात नहीं है। मैं दोबारा प्रयास नहीं करूँगी। मिलान चल रहा है "
            "और मैं ठीक वही बताऊँगी जो Razorpay पुष्टि करेगा।"
        ),
        RecoveryCode.STALE_CAPTURE: (
            "एक ऐसे चेकआउट संस्करण के लिए राशि प्राप्त हुई जो अब मान्य नहीं है। इसे संभाला जा रहा "
            "है। इसके बदले कोई ऑर्डर पूरा नहीं होगा।"
        ),
        RecoveryCode.REFUND_ALLOWED: (
            "{amount_phrase} के रिफ़ंड का अनुरोध किया गया है। यह अभी पूरा नहीं हुआ है।"
        ),
        RecoveryCode.REFUND_REVIEW_REQUIRED: (
            "{amount_phrase} के रिफ़ंड को आगे बढ़ाने से पहले समीक्षा चाहिए। अभी तक कुछ भी रिफ़ंड नहीं हुआ है।"
        ),
        RecoveryCode.RESOLUTION_PLAN_ISSUED: (
            "एक समाधान योजना जारी की गई है। इसे स्क्रीन पर देखें। जब तक आप स्वीकार नहीं करते, कुछ नहीं बदलता।"
        ),
        RecoveryCode.RESOLUTION_PLAN_EXPIRED: (
            "समाधान योजना की अवधि समाप्त हो गई है। कोई बदलाव नहीं हुआ।"
        ),
        RecoveryCode.RECONCILIATION_IN_PROGRESS: (
            "Razorpay के साथ मिलान चल रहा है। जब तक यह पूरा नहीं होता, मैं दोबारा प्रयास नहीं करूँगी।"
        ),
        RecoveryCode.POLICY_EXCEPTION: (
            "व्यापारी की नीति इस कार्रवाई की अनुमति नहीं देती। कोई बदलाव नहीं हुआ।"
        ),
        RecoveryCode.HUMAN_REVIEW_REQUIRED: (
            "कुछ भी होने से पहले इसकी मानवीय समीक्षा ज़रूरी है। कोई बदलाव नहीं हुआ।"
        ),
        RecoveryCode.CONNECTOR_UNAVAILABLE: (
            "व्यापारी का सिस्टम जवाब नहीं दे रहा। कुछ भी नहीं भेजा गया और कोई भुगतान नहीं हुआ। "
            "जो क़ीमत मैं पक्की नहीं कर सकती, वह मैं नहीं बताऊँगी।"
        ),
        RecoveryCode.SAFE_MODE_ACTIVE: (
            "सुरक्षित मोड सक्रिय है। धन संबंधी कार्रवाइयाँ रुकी हुई हैं। कोई बदलाव नहीं हुआ।"
        ),
    },
}

# Keyed by the kernel's stable reason key (``KernelDecision.explanation``); overrides the
# code template when a more specific sentence exists. Keys come from the kernel source.
_BY_REASON: Final[dict[Locale, dict[str, str]]] = {
    Locale.EN_IN: {
        "a_newer_version_exists": (
            "A newer version of this checkout already exists. Version {version} cannot be "
            "paid. Review the latest version on screen."
        ),
        "principal_lacks_submit_capability": (
            "This assistant is not permitted to submit payments. Approve on screen to "
            "continue. No payment was made."
        ),
        "reservation_not_valid": (
            "The reservation for version {version} is no longer valid. No payment was made. "
            "Review availability on screen to continue."
        ),
    },
    Locale.HI_IN: {
        "a_newer_version_exists": (
            "इस चेकआउट का एक नया संस्करण पहले से मौजूद है। संस्करण {version} का भुगतान नहीं हो "
            "सकता। स्क्रीन पर नवीनतम संस्करण देखें।"
        ),
        "principal_lacks_submit_capability": (
            "इस सहायक को भुगतान भेजने की अनुमति नहीं है। जारी रखने के लिए स्क्रीन पर स्वीकृति दें। "
            "कोई भुगतान नहीं हुआ।"
        ),
        "reservation_not_valid": (
            "संस्करण {version} का आरक्षण अब मान्य नहीं है। कोई भुगतान नहीं हुआ। जारी रखने के लिए "
            "स्क्रीन पर उपलब्धता देखें।"
        ),
    },
}

# One sentence per Delta, keyed by the kernel's delta reason; a generic fallback covers
# any reason this module has not seen, so no delta is ever silently omitted.
_DELTA_BY_REASON: Final[dict[Locale, dict[str, str]]] = {
    Locale.EN_IN: {
        "total_changed": "The total changed from {approved} to {current}.",
        "availability_changed": "An item you approved is no longer available.",
        "GRANT_BINDING": (
            "The {field} differs from what was admitted: {approved} was admitted, "
            "{current} was attempted."
        ),
    },
    Locale.HI_IN: {
        "total_changed": "कुल राशि {approved} से बदलकर {current} हो गई।",
        "availability_changed": "आपने जो वस्तु स्वीकृत की थी, वह अब उपलब्ध नहीं है।",
        "GRANT_BINDING": ("{field} स्वीकृत मान से अलग है: {approved} स्वीकृत था, {current} का प्रयास हुआ।"),
    },
}
_DELTA_GENERIC: Final[dict[Locale, str]] = {
    Locale.EN_IN: "The {field} changed from {approved} to {current}.",
    Locale.HI_IN: "{field} {approved} से बदलकर {current} हो गया।",
}

#: Delta field paths whose values are integer minor units of the decision's currency.
_MONEY_FIELDS: Final[frozenset[str]] = frozenset({"total", "amount", "amount_minor"})


def _delta_value(delta: Delta, value: object, currency: str, locale: Locale) -> str:
    if isinstance(value, Money):
        return spoken_amount(value, locale)
    if delta.field_path in _MONEY_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return spoken_amount(Money(value, currency), locale)
    return str(value)


def render_delta(delta: Delta, *, locale: Locale, currency: str = "INR") -> str:
    """One sentence for one material difference. Never omitted, never model-authored."""
    template = _DELTA_BY_REASON[locale].get(delta.reason, _DELTA_GENERIC[locale])
    return template.format(
        field=delta.field_path.replace("_", " "),
        approved=_delta_value(delta, delta.approved, currency, locale),
        current=_delta_value(delta, delta.current, currency, locale),
    )


def render_decision(
    decision: KernelDecision,
    *,
    locale: Locale = Locale.EN_IN,
    amount: Money | None = None,
    previous_amount: Money | None = None,
) -> RenderedSpeech:
    """Render a ``KernelDecision`` into deterministic speech with its audit fields.

    ``amount`` and ``previous_amount`` are server-confirmed figures from the trusted
    approval card. When absent, the sentence names "the approved amount" rather than
    inventing a figure; a template never guesses a number.
    """
    currency = amount.currency if amount is not None else "INR"
    reason_key = decision.explanation
    body = _BY_REASON[locale].get(reason_key)
    template_id = f"decision.{reason_key}" if body is not None else f"decision.{decision.code}"
    if body is None:
        body = _BY_CODE[locale][decision.code]

    deltas_text = " ".join(
        render_delta(delta, locale=locale, currency=currency) for delta in decision.deltas
    )
    version = (
        str(decision.checkout.version) if decision.checkout is not None else _ABSENT_VERSION[locale]
    )
    next_version = (
        str(decision.next_version) if decision.next_version is not None else _ABSENT_VERSION[locale]
    )
    amount_phrase = spoken_amount(amount, locale) if amount is not None else _ABSENT_AMOUNT[locale]
    previous_phrase = (
        spoken_amount(previous_amount, locale)
        if previous_amount is not None
        else _ABSENT_PREVIOUS[locale]
    )
    text = body.format(
        version=version,
        next_version=next_version,
        amount_phrase=amount_phrase,
        previous_amount_phrase=previous_phrase,
        deltas=f"{deltas_text} " if deltas_text else "",
    ).strip()

    fields: dict[str, str] = {
        "decision_id": str(decision.decision_id),
        "code": str(decision.code),
        "reason_key": reason_key,
        "allowed": str(decision.allowed).lower(),
        "version": version,
        "next_version": next_version,
        "currency": currency,
        "delta_count": str(len(decision.deltas)),
    }
    if decision.checkout is not None:
        fields["checkout_id"] = str(decision.checkout.checkout_id)
        fields["content_hash"] = decision.checkout.content_hash
    if amount is not None:
        fields["amount_minor"] = str(amount.minor)
        fields["amount_digits"] = format_money_digits(amount)
        fields["amount_words"] = money_to_words(amount, locale)
    if previous_amount is not None:
        fields["previous_amount_minor"] = str(previous_amount.minor)
        fields["previous_amount_digits"] = format_money_digits(previous_amount)
        fields["previous_amount_words"] = money_to_words(previous_amount, locale)
    for index, delta in enumerate(decision.deltas):
        fields[f"delta.{index}.field_path"] = delta.field_path
        fields[f"delta.{index}.reason"] = delta.reason
        fields[f"delta.{index}.approved"] = str(delta.approved)
        fields[f"delta.{index}.current"] = str(delta.current)
    if decision.grant_id is not None:
        fields["grant_id"] = str(decision.grant_id)
    if decision.payment_attempt_id is not None:
        fields["payment_attempt_id"] = str(decision.payment_attempt_id)

    return RenderedSpeech(
        text=text,
        template_id=template_id,
        template_version=TEMPLATE_VERSION,
        locale=locale,
        fields=fields,
    )


# ---- the approval card, read aloud --------------------------------------------------------
#
# This is the consent surface for the voice path, exactly as the on-screen card is for
# the button: version, then the amount as digits AND words, then the ask. A buyer who
# agrees to "your order" has agreed to nothing in particular; a buyer who heard "Version
# 2, ₹395.00, three hundred ninety-five rupees" and said yes has agreed to those bytes.
#
# Both sentences carry words outside the consent lexicon on purpose ("say", "to", "or";
# "कहें", "या"). If the reading ever leaked back through the microphone, the subset rule
# in ``voice_runtime.consent`` refuses it as a near-miss rather than hearing its own
# "yes" as the buyer's.

CONSENT_TEMPLATE_ID: Final[str] = "consent.read_card"

_CONSENT: Final[dict[Locale, str]] = {
    Locale.EN_IN: (
        "Version {version}, {amount_digits}, {amount_words}. Say yes to approve this exact "
        "version, or no to decline."
    ),
    Locale.HI_IN: (
        "संस्करण {version}, {amount_digits}, {amount_words}। इसी संस्करण को स्वीकृत करने के लिए "
        "हाँ कहें, या मना करने के लिए नहीं।"
    ),
}


def render_consent_reading(
    *,
    checkout_id: str,
    version: int,
    content_hash: str,
    amount: Money,
    locale: Locale = Locale.EN_IN,
) -> RenderedSpeech:
    """The approval card as deterministic speech, with the fields consent binds to.

    Every argument comes from the trusted server's own card. The hash is not spoken --
    nobody can hear forty-three characters of base64 -- but it travels in the audit fields
    beside what was, so the record of the reading names the bytes it was a reading of.
    """
    text = _CONSENT[locale].format(
        version=version,
        amount_digits=format_money_digits(amount),
        amount_words=money_to_words(amount, locale),
    )
    return RenderedSpeech(
        text=text,
        template_id=CONSENT_TEMPLATE_ID,
        template_version=TEMPLATE_VERSION,
        locale=locale,
        fields={
            "checkout_id": checkout_id,
            "version": str(version),
            "content_hash": content_hash,
            "amount_minor": str(amount.minor),
            "amount_digits": format_money_digits(amount),
            "amount_words": money_to_words(amount, locale),
            "currency": amount.currency,
        },
    )


def template_ids() -> frozenset[str]:
    """Every template ID this version can emit; used by audit consumers and tests."""
    ids = {f"decision.{code}" for code in RecoveryCode}
    ids |= {f"decision.{key}" for key in _BY_REASON[Locale.EN_IN]}
    ids.add(CONSENT_TEMPLATE_ID)
    return frozenset(ids)


# ---- rendering from a decision CARD ---------------------------------------------------
#
# ``agent_runtime.rendering.cards.decision_card`` is the JSON the platform's own
# ``present_decision`` tool produces, and it carries every field these templates need:
# ``code``, ``explanation``, ``allowed``, ``next_version``, ``current_version``, ``total``
# as ``{"minor", "currency", "display"}``, and ``items`` as the deltas.
#
# Rendering from the card rather than reconstructing a ``KernelDecision`` is deliberate.
# The kernel's own type refuses a decision that is allowed and names no Execution Grant --
# rightly, because every provider mutation consumes exactly one -- and the card does not
# carry the grant id. Faking one to satisfy a constructor would be inventing a fact about
# money to make a renderer happy, which is the opposite of what this module is for.
#
# The same template tables serve both entry points, so there is one set of sentences, not
# two that drift.


def _deltas_of(card: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The delta list, under either key the platform uses.

    ``agent_runtime.rendering.cards.decision_card`` puts them in ``items`` (its envelope
    shape); ``commerce_api``'s checkout-derived card puts them in ``deltas``. The field
    shape is identical either way. Reading only one key would have spoken a refusal with
    no deltas in it, which is the "something changed" summary 19.10 exists to prevent.
    """
    for key in ("deltas", "items"):
        found = card.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, Mapping)]
    return []


def _delta_of(item: Mapping[str, Any]) -> Delta:
    """A card's delta as the kernel's own type, so ONE renderer serves both paths."""
    return Delta(
        field_path=str(item.get("field_path", "")),
        approved=item.get("approved"),
        current=item.get("current"),
        reason=str(item.get("reason", "")),
    )


#: Field paths that name the checkout total. The kernel writes ``total``; the in-memory
#: backend writes ``total_minor``. Both mean paise (see ``rendering/money.py`` upstream).
_TOTAL_FIELDS: Final[frozenset[str]] = frozenset({"total", "total_minor"})


def _previous_total(deltas: list[Mapping[str, Any]], currency: str) -> Money | None:
    """The superseded total, taken from the delta the server sent -- never computed.

    The reapproval template names the amount the buyer originally approved, and a card
    derived from a checkout read does not carry it as a field. It does carry it inside the
    total delta's ``approved`` side, which is the kernel's own number for exactly that.
    """
    for item in deltas:
        approved = item.get("approved")
        named_total = str(item.get("field_path", "")) in _TOTAL_FIELDS
        if named_total and isinstance(approved, int) and not isinstance(approved, bool):
            return Money(minor=approved, currency=currency)
    return None


def render_decision_card(
    card: Mapping[str, Any], *, locale: Locale = Locale.EN_IN
) -> RenderedSpeech:
    """Deterministic speech for a ``decision`` card, with its audit fields.

    TWO KINDS OF CARD, AND WHY ``source`` IS READ RATHER THAN ASSUMED
    ----------------------------------------------------------------
    ``source: "kernel_decision"`` (or absent) is a card carried back from an admission the
    kernel actually performed: it names a ``decision_id`` and the kernel's own
    ``explanation``. ``source: "checkout_state"`` is derived from a checkout read -- a live
    version carrying a ``previous_version`` and a non-empty delta list, which by
    construction replaced an approval the merchant's state had outgrown. No admission ran,
    so ``decision_id`` and ``explanation`` are ``null``.

    Both are true statements about the same superseded approval, and both are spoken from
    the same template. What must not happen is speaking the second as though it were the
    first, so ``source`` is recorded in the audit fields and the null identifiers are
    omitted rather than rendered as the string "None" -- which is what the first version of
    this function did, and it would have written ``decision_id: "None"`` into the audit
    trail beside a spoken money fact.

    Raises :class:`KeyError` if the card names a code these templates do not cover, which
    is the correct failure: a decision this module cannot say is one the buyer must read.
    """
    total = card.get("total")
    currency = str(total.get("currency", "INR")) if isinstance(total, Mapping) else "INR"
    amount = (
        Money(minor=int(total["minor"]), currency=currency)
        if isinstance(total, Mapping) and isinstance(total.get("minor"), int)
        else None
    )
    deltas = _deltas_of(card)
    previous = _previous_total(deltas, currency)

    explanation = card.get("explanation")
    reason_key = str(explanation) if isinstance(explanation, str) and explanation else ""
    body = _BY_REASON[locale].get(reason_key) if reason_key else None
    code = str(card.get("code", ""))
    template_id = f"decision.{reason_key}" if body is not None else f"decision.{code}"
    if body is None:
        body = _BY_CODE[locale][RecoveryCode(code)]

    deltas_text = " ".join(
        render_delta(_delta_of(item), locale=locale, currency=currency) for item in deltas
    )
    version = str(card.get("current_version") or _ABSENT_VERSION[locale])
    next_version = str(card.get("next_version") or _ABSENT_VERSION[locale])
    text = body.format(
        version=version,
        next_version=next_version,
        amount_phrase=spoken_amount(amount, locale) if amount else _ABSENT_AMOUNT[locale],
        previous_amount_phrase=(
            spoken_amount(previous, locale) if previous else _ABSENT_PREVIOUS[locale]
        ),
        deltas=f"{deltas_text} " if deltas_text else "",
    ).strip()

    fields: dict[str, str] = {
        "code": code,
        "allowed": str(bool(card.get("allowed"))).lower(),
        "version": version,
        "next_version": next_version,
        "currency": currency,
        "delta_count": str(len(deltas)),
        # Which kind of card this was. A consumer of the audit trail can tell a sentence
        # spoken from an admission apart from one spoken from a checkout read.
        "source": str(card.get("source") or "kernel_decision"),
    }
    # Null identifiers are OMITTED, not stringified. "None" in an audit field beside a
    # money fact is worse than an absent one, because it reads like a value.
    if isinstance(card.get("decision_id"), str) and card["decision_id"]:
        fields["decision_id"] = card["decision_id"]
    if reason_key:
        fields["reason_key"] = reason_key
    if card.get("checkout_id"):
        fields["checkout_id"] = str(card["checkout_id"])
    if card.get("previous_version") is not None:
        fields["previous_version"] = str(card["previous_version"])
    if amount is not None:
        fields["amount_minor"] = str(amount.minor)
        fields["amount_digits"] = format_money_digits(amount)
        fields["amount_words"] = money_to_words(amount, locale)
    if previous is not None:
        fields["previous_amount_minor"] = str(previous.minor)
        fields["previous_amount_digits"] = format_money_digits(previous)
    for index, item in enumerate(deltas):
        fields[f"delta.{index}.field_path"] = str(item.get("field_path", ""))
        fields[f"delta.{index}.reason"] = str(item.get("reason", ""))
        fields[f"delta.{index}.approved"] = str(item.get("approved"))
        fields[f"delta.{index}.current"] = str(item.get("current"))

    return RenderedSpeech(
        text=text,
        template_id=template_id,
        template_version=TEMPLATE_VERSION,
        locale=locale,
        fields=fields,
    )

"""Deterministic buyer-facing sentences. No model authors any of these.

Specification 19.10 lists what a model may never write: approval scope, the items and
quantities being confirmed, the total, a material price or fee delta, reservation expiry,
payment state, cancellation effect, refund amount and status, and anything about delegated
authority. Those sentences are rendered here, from versioned templates filled with fields
a deterministic service returned.

WHY A TABLE AND NOT PROSE FROM THE MODEL
----------------------------------------
A model asked to "explain the refusal" will summarise. Summarising a refusal is exactly
where a buyer stops seeing the delta that made it a refusal, and a buyer who does not see
the delta has not consented to the new price. So the refusal text is a loop over
``decision.deltas`` that cannot skip one, and the invalidation sentence is a constant.

WHY EVERY RECOVERY CODE IS PRESENT
-----------------------------------
:data:`RECOVERY_TEXT` covers every member of
:class:`commerce_domain.RecoveryCode` in every language, and a test iterates the enum
to prove it. A missing entry would otherwise degrade silently into the code name -- the
buyer would read ``AUTHORITY_REVOKED`` -- which is the failure mode a table is supposed to
prevent.

Money never appears here as a literal. Every amount arrives as integer minor units from a
tool result and is formatted by :mod:`agent_runtime.rendering.money`. This module performs
no arithmetic on money at all; the only number it computes is a version successor.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from commerce_domain import AdmissionDecision, RecoveryCode

from ..language import Language
from .money import display_delta_value, display_minor

__all__ = [
    "REASON_TEXT",
    "RECOVERY_TEXT",
    "reason_text",
    "recovery_text",
    "render_decision",
    "render_denial",
    "render_fallback",
    "render_reasoning_unavailable",
    "render_unverified",
]

#: Template family version, recorded beside a spoken money event (specification 19.10).
TEMPLATE_VERSION: Final[str] = "1"


def _tri(en: str, hi: str, hi_latn: str) -> Mapping[Language, str]:
    """One line in three languages. Read-only, so a caller cannot patch a template."""
    return MappingProxyType({Language.EN: en, Language.HI: hi, Language.HI_LATN: hi_latn})


# --------------------------------------------------------------------- recovery codes

RECOVERY_TEXT: Final[Mapping[RecoveryCode, Mapping[Language, str]]] = MappingProxyType(
    {
        RecoveryCode.OK: _tri(
            "Accepted.",
            "स्वीकार कर लिया गया।",
            "Accept ho gaya.",
        ),
        RecoveryCode.DUPLICATE_OPERATION: _tri(
            "This was already done once. You are seeing the original result, not a second one.",
            "यह पहले ही एक बार हो चुका है। आपको वही पहला परिणाम दिख रहा है, दूसरा नहीं।",
            "Yeh pehle hi ek baar ho chuka hai. Aapko wahi pehla result dikh raha hai, "
            "doosra nahi.",
        ),
        RecoveryCode.CONCURRENT_OPERATION: _tri(
            "Another action on this checkout is already running. Nothing new was started.",
            "इस चेकआउट पर पहले से एक कार्रवाई चल रही है। कुछ भी नया शुरू नहीं किया गया।",
            "Is checkout par pehle se ek action chal raha hai. Kuch bhi naya shuru nahi kiya gaya.",
        ),
        RecoveryCode.STALE_CHECKOUT: _tri(
            "The version that was submitted is no longer the current one.",
            "जो संस्करण भेजा गया था वह अब मौजूदा नहीं रहा।",
            "Jo version bheja gaya tha wo ab current nahi raha.",
        ),
        RecoveryCode.REAPPROVAL_REQUIRED: _tri(
            "Something material changed, so this needs your approval again before it can go on.",
            "कुछ महत्वपूर्ण बदल गया है, इसलिए आगे बढ़ने से पहले इसे फिर से आपकी मंज़ूरी चाहिए।",
            "Kuch important badal gaya hai, isliye aage badhne se pehle iske liye aapki "
            "approval dobara chahiye.",
        ),
        RecoveryCode.RESERVATION_EXPIRED: _tri(
            "The hold on these items has expired. They have to be reserved and priced again.",
            "इन वस्तुओं पर लगी रोक समाप्त हो गई है। इन्हें दोबारा आरक्षित और मूल्यांकित करना होगा।",
            "In items par laga hold khatam ho gaya hai. Inko dobara reserve aur price karna hoga.",
        ),
        RecoveryCode.SOLD_OUT: _tri(
            "Everything in this order sold out before it was paid for. "
            "You have not been charged, and the items have been released.",
            "इस ऑर्डर की सारी वस्तुएँ भुगतान से पहले ही समाप्त हो गईं। "
            "आपसे कोई शुल्क नहीं लिया गया है, और वस्तुएँ छोड़ दी गई हैं।",
            "Is order ka saara saamaan payment se pehle hi khatam ho gaya. "
            "Aapse koi paisa nahi liya gaya, aur items chhod diye gaye hain.",
        ),
        RecoveryCode.AUTHORITY_REVOKED: _tri(
            "The authority for this action has been withdrawn, so execution stopped here.",
            "इस कार्रवाई का अधिकार वापस ले लिया गया है, इसलिए काम यहीं रोक दिया गया।",
            "Is action ka authority wapas le liya gaya hai, isliye kaam yahin ruk gaya.",
        ),
        RecoveryCode.AUTHORITY_INSUFFICIENT: _tri(
            "The approval on file does not cover this action. A correctly scoped one is needed.",
            "जो मंज़ूरी दर्ज है वह इस कार्रवाई को नहीं ढकती। सही दायरे वाली मंज़ूरी चाहिए।",
            "Jo approval record mein hai wo is action ko cover nahi karti. Sahi scope wali "
            "approval chahiye.",
        ),
        RecoveryCode.PAYMENT_FAILED: _tri(
            "Razorpay reported that the attempt failed. No money left your account.",
            "Razorpay ने बताया कि प्रयास विफल रहा। आपके खाते से कोई राशि नहीं गई।",
            "Razorpay ne bataya ki attempt fail ho gaya. Aapke account se koi paisa nahi gaya.",
        ),
        RecoveryCode.PAYMENT_PENDING: _tri(
            "Razorpay still has this in a non-final state. Nothing is fulfilled until it settles.",
            "Razorpay के पास यह अभी भी अंतिम स्थिति में नहीं है। तय होने तक कुछ भी पूरा नहीं होगा।",
            "Razorpay ke paas yeh abhi bhi final state mein nahi hai. Tay hone tak kuch bhi "
            "poora nahi hoga.",
        ),
        RecoveryCode.PAYMENT_UNKNOWN: _tri(
            "The outcome is not known yet. It is being reconciled with Razorpay and will not "
            "be retried blindly.",
            "नतीजा अभी ज्ञात नहीं है। इसका मिलान Razorpay से किया जा रहा है और इसे बिना जाने "
            "दोबारा नहीं आज़माया जाएगा।",
            "Nateeja abhi pata nahi hai. Iska milaan Razorpay se kiya ja raha hai aur bina "
            "jaane dobara try nahi kiya jayega.",
        ),
        RecoveryCode.STALE_CAPTURE: _tri(
            "Money landed against a checkout that was already invalid. Fulfilment is blocked "
            "and a refund starts on its own.",
            "राशि ऐसे चेकआउट पर आई जो पहले ही अमान्य हो चुका था। पूर्ति रोक दी गई है और "
            "धनवापसी अपने आप शुरू हो रही है।",
            "Paisa aise checkout par aaya jo pehle hi invalid ho chuka tha. Fulfilment rok di "
            "gayi hai aur refund apne aap shuru ho raha hai.",
        ),
        RecoveryCode.REFUND_ALLOWED: _tri(
            "A refund is admissible here. It still needs your confirmation on the trusted screen.",
            "यहाँ धनवापसी स्वीकार्य है। इसके लिए भरोसेमंद स्क्रीन पर आपकी पुष्टि अब भी चाहिए।",
            "Yahan refund allowed hai. Iske liye trusted screen par aapki confirmation abhi "
            "bhi chahiye.",
        ),
        RecoveryCode.REFUND_REVIEW_REQUIRED: _tri(
            "This refund needs review before it can be issued. The evidence has been attached.",
            "यह धनवापसी जारी होने से पहले समीक्षा माँगती है। प्रमाण साथ लगा दिए गए हैं।",
            "Yeh refund issue hone se pehle review maangta hai. Evidence saath laga diya gaya hai.",
        ),
        RecoveryCode.RESOLUTION_PLAN_ISSUED: _tri(
            "A resolution plan has been issued with fixed options. Choose one of them.",
            "एक समाधान योजना निश्चित विकल्पों के साथ जारी की गई है। उनमें से एक चुनिए।",
            "Ek resolution plan fixed options ke saath jaari kiya gaya hai. Unmein se ek chuniye.",
        ),
        RecoveryCode.RESOLUTION_PLAN_EXPIRED: _tri(
            "That plan expired before it was confirmed. A fresh one has to be worked out; an "
            "expired plan is never reused.",
            "वह योजना पुष्टि से पहले समाप्त हो गई। नई योजना बनानी होगी; समाप्त योजना दोबारा "
            "कभी इस्तेमाल नहीं होती।",
            "Wo plan confirm hone se pehle expire ho gaya. Naya plan banana hoga; expire hua "
            "plan dobara kabhi use nahi hota.",
        ),
        RecoveryCode.RECONCILIATION_IN_PROGRESS: _tri(
            "Razorpay's record is being checked right now. No retry and no refund can happen "
            "until that finishes.",
            "अभी Razorpay का रिकॉर्ड जाँचा जा रहा है। यह पूरा होने तक न दोबारा प्रयास होगा और न धनवापसी।",
            "Abhi Razorpay ka record check ho raha hai. Yeh poora hone tak na dobara try hoga "
            "na refund.",
        ),
        RecoveryCode.POLICY_EXCEPTION: _tri(
            "Standard policy cannot settle this, so it is going up with the rule that applies.",
            "मानक नीति इसे नहीं सुलझा सकती, इसलिए इसे लागू नियम के साथ आगे भेजा जा रहा है।",
            "Standard policy ise solve nahi kar sakti, isliye ise lagu rule ke saath aage bheja "
            "ja raha hai.",
        ),
        RecoveryCode.HUMAN_REVIEW_REQUIRED: _tri(
            "This stops here on purpose and goes to a person. A support case was created with "
            "the verified facts.",
            "यह जानबूझकर यहीं रुकता है और किसी व्यक्ति के पास जाता है। सत्यापित तथ्यों के साथ "
            "एक सहायता प्रकरण बनाया गया है।",
            "Yeh jaanbujh kar yahin rukta hai aur ek insaan ke paas jata hai. Verified facts "
            "ke saath ek support case banaya gaya hai.",
        ),
        RecoveryCode.CONNECTOR_UNAVAILABLE: _tri(
            "The merchant's system is not answering, so this stopped before any payment. "
            "Nothing was charged, and no price here can be confirmed until it responds.",
            "विक्रेता का सिस्टम जवाब नहीं दे रहा, इसलिए यह भुगतान से पहले ही रुक गया। कोई राशि "
            "नहीं ली गई, और जब तक वह जवाब न दे तब तक यहाँ की कोई क़ीमत पक्की नहीं मानी जा सकती।",
            "Seller ka system jawab nahi de raha, isliye yeh payment se pehle hi ruk gaya. "
            "Koi paisa nahi liya gaya, aur jab tak wo jawab na de tab tak yahan ki koi "
            "keemat pakki nahi maani ja sakti.",
        ),
        RecoveryCode.SAFE_MODE_ACTIVE: _tri(
            "The platform is in safe mode, so this operation is paused. Nothing was charged.",
            "मंच सुरक्षित मोड में है, इसलिए यह कार्रवाई रोकी गई है। कोई राशि नहीं ली गई।",
            "Platform safe mode mein hai, isliye yeh action roka gaya hai. Koi paisa nahi liya "
            "gaya.",
        ),
    }
)


def recovery_text(code: RecoveryCode, language: Language) -> str:
    """The buyer-facing sentence for one recovery code.

    Falls back to English rather than to the bare code name: an untranslated sentence is a
    smaller failure than a buyer reading ``AUTHORITY_INSUFFICIENT``. The completeness test
    is what keeps this fallback from ever running in practice.
    """
    entry = RECOVERY_TEXT.get(code)
    if entry is None:  # pragma: no cover - the enum-completeness test forbids this
        return code.value
    return entry.get(language, entry[Language.EN])


# ------------------------------------------------------------------------ delta reasons

REASON_TEXT: Final[Mapping[str, Mapping[Language, str]]] = MappingProxyType(
    {
        "PRICE_CHANGED": _tri("the price changed", "क़ीमत बदल गई", "price badal gaya"),
        "QUANTITY_REDUCED": _tri(
            "fewer units are available", "कम मात्रा उपलब्ध है", "kam quantity available hai"
        ),
        "ITEM_UNAVAILABLE": _tri(
            "the item is out of stock", "वस्तु स्टॉक में नहीं है", "item stock mein nahi hai"
        ),
        "ITEM_DELISTED": _tri(
            "the merchant delisted the item",
            "विक्रेता ने वस्तु हटा दी",
            "seller ne item hata diya",
        ),
        "TAX_CHANGED": _tri("the tax changed", "कर बदल गया", "tax badal gaya"),
        "SUBTOTAL_CHANGED": _tri(
            "the items subtotal changed", "वस्तुओं का उप-योग बदल गया", "items ka subtotal badal gaya"
        ),
        "DELIVERY_FEE_CHANGED": _tri(
            "the delivery fee changed", "डिलीवरी शुल्क बदल गया", "delivery fee badal gayi"
        ),
        "DELIVERY_TAX_CHANGED": _tri(
            "the tax on delivery changed",
            "डिलीवरी पर कर बदल गया",
            "delivery par tax badal gaya",
        ),
        "TOTAL_CHANGED": _tri("the total changed", "कुल राशि बदल गई", "total badal gaya"),
        "FREE_DELIVERY_CHANGED": _tri(
            "free delivery no longer applies the same way",
            "मुफ़्त डिलीवरी अब पहले जैसी लागू नहीं होती",
            "free delivery ab pehle jaisi lagu nahi hoti",
        ),
        "NOT_QUOTABLE": _tri(
            "the merchant can no longer price this cart",
            "विक्रेता अब इस cart का मूल्य नहीं बता सकता",
            "seller ab is cart ka price nahi bata sakta",
        ),
        "AVAILABILITY_CHANGED": _tri(
            "availability changed", "उपलब्धता बदल गई", "availability badal gayi"
        ),
        "EVIDENCE_MISMATCH": _tri(
            "the provider's record does not match this attempt",
            "प्रदाता का रिकॉर्ड इस प्रयास से मेल नहीं खाता",
            "provider ka record is attempt se match nahi karta",
        ),
        "PAYMENT_ID_RECORDED": _tri(
            "the provider's payment reference was recorded",
            "प्रदाता का भुगतान संदर्भ दर्ज कर लिया गया",
            "provider ka payment reference record kar liya gaya",
        ),
        "PAYMENT_ID_ALREADY_RECORDED": _tri(
            "that payment reference was already recorded",
            "वह भुगतान संदर्भ पहले ही दर्ज है",
            "wo payment reference pehle hi record hai",
        ),
        "DIFFERENT_PAYMENT_ID_ALREADY_RECORDED": _tri(
            "a different payment reference is already recorded against this attempt",
            "इस प्रयास पर पहले से कोई दूसरा भुगतान संदर्भ दर्ज है",
            "is attempt par pehle se koi doosra payment reference record hai",
        ),
        "ORDER_NOT_BOUND_TO_ATTEMPT": _tri(
            "that provider order does not belong to this attempt",
            "वह प्रदाता ऑर्डर इस प्रयास का नहीं है",
            "wo provider order is attempt ka nahi hai",
        ),
    }
)


def reason_text(reason: str, language: Language) -> str:
    """Render a :class:`commerce_domain.Delta` reason key in the buyer's language.

    Case-insensitive, because the kernel writes ``total_changed`` and the service layer
    writes ``TOTAL_CHANGED`` for the same fact. An unknown key degrades to a readable
    phrase rather than raising: a reason the platform has not translated yet must still
    reach the buyer beside the numbers, since the numbers are what consent attaches to.
    """
    entry = REASON_TEXT.get(reason.upper())
    if entry is None:
        return reason.replace("_", " ").casefold()
    return entry.get(language, entry[Language.EN])


# --------------------------------------------------------------------------- fragments

_ADMITTED: Final[Mapping[Language, str]] = _tri(
    "Admitted. This is not a payment: the platform now holds permission for exactly one "
    "payment attempt, and Razorpay's own record decides the outcome.",
    "स्वीकृत। यह भुगतान नहीं है: अब मंच के पास ठीक एक भुगतान प्रयास की अनुमति है, और नतीजा "
    "Razorpay का अपना रिकॉर्ड तय करेगा।",
    "Admit ho gaya. Yeh payment nahi hai: ab platform ke paas theek ek payment attempt ki "
    "ijazat hai, aur nateeja Razorpay ka apna record tay karega.",
)

_CHANGED_HEADER: Final[Mapping[Language, str]] = _tri(
    "What changed since you approved:",
    "आपकी मंज़ूरी के बाद क्या बदला:",
    "Aapke approve karne ke baad kya badla:",
)

_APPROVED_LABEL: Final[Mapping[Language, str]] = _tri("approved", "मंज़ूर", "approve kiya tha")
_CURRENT_LABEL: Final[Mapping[Language, str]] = _tri("now", "अब", "ab")

_INVALIDATED_WITH_NEXT: Final[Mapping[Language, str]] = _tri(
    "Version {n} is invalidated and can never be approved again. Version {m} carries the "
    "current facts and needs your approval on the trusted screen.",
    "संस्करण {n} अमान्य कर दिया गया है और दोबारा कभी मंज़ूर नहीं हो सकता। संस्करण {m} में "
    "मौजूदा जानकारी है और उसे भरोसेमंद स्क्रीन पर आपकी मंज़ूरी चाहिए।",
    "Version {n} invalid kar diya gaya hai aur dobara kabhi approve nahi ho sakta. Version "
    "{m} mein abhi ki jaankari hai aur usko trusted screen par aapki approval chahiye.",
)

_INVALIDATED_ONLY: Final[Mapping[Language, str]] = _tri(
    "Version {n} is invalidated and can never be approved again.",
    "संस्करण {n} अमान्य कर दिया गया है और दोबारा कभी मंज़ूर नहीं हो सकता।",
    "Version {n} invalid kar diya gaya hai aur dobara kabhi approve nahi ho sakta.",
)

_NOTHING_CHARGED: Final[Mapping[Language, str]] = _tri(
    "Nothing has been charged.",
    "कोई राशि नहीं ली गई है।",
    "Koi paisa nahi liya gaya hai.",
)

_CANNOT_APPROVE: Final[Mapping[Language, str]] = _tri(
    "I cannot approve this for you; approval happens on the trusted screen.",
    "मैं आपके लिए इसे मंज़ूर नहीं कर सकता; मंज़ूरी भरोसेमंद स्क्रीन पर होती है।",
    "Main aapke liye ise approve nahi kar sakta; approval trusted screen par hoti hai.",
)


def render_decision(
    decision: AdmissionDecision, language: Language, *, currency: str = "INR"
) -> str:
    """The buyer's whole view of one kernel decision. The hero moment of the refusal path.

    On a refusal every delta appears -- field path, the value that was approved, the value
    that is true now, and why -- because a summary of a refusal is how a buyer ends up
    consenting to a number nobody showed them (specification 6.3). The version sentence is
    a constant: N is invalidated, N+1 needs approval.
    """
    if decision.allowed:
        return _ADMITTED[language]

    parts: list[str] = [recovery_text(decision.code, language)]

    if decision.deltas:
        approved_label = _APPROVED_LABEL[language]
        current_label = _CURRENT_LABEL[language]
        parts.append(_CHANGED_HEADER[language])
        for delta in decision.deltas:
            was = display_delta_value(delta.field_path, delta.approved, currency)
            now = display_delta_value(delta.field_path, delta.current, currency)
            parts.append(
                f"- {delta.field_path}: {approved_label} {was}, {current_label} {now}"
                f" ({reason_text(delta.reason, language)})"
            )

    version = decision.checkout.version if decision.checkout is not None else None
    if version is not None:
        successor = decision.next_version
        if successor is None and decision.code is RecoveryCode.REAPPROVAL_REQUIRED:
            successor = version + 1
        if successor is not None:
            parts.append(_INVALIDATED_WITH_NEXT[language].format(n=version, m=successor))
            parts.append(_CANNOT_APPROVE[language])
        elif decision.deltas:
            parts.append(_INVALIDATED_ONLY[language].format(n=version))

    parts.append(_NOTHING_CHARGED[language])
    return "\n".join(parts)


# ---------------------------------------------------------------------------- denials

_DENIAL_TEXT: Final[Mapping[str, Mapping[Language, str]]] = MappingProxyType(
    {
        "capability_missing": _tri(
            "I am not allowed to do that. That action is not part of what this assistant "
            "may ever do, whatever it is asked.",
            "मुझे ऐसा करने की अनुमति नहीं है। यह कार्रवाई इस सहायक के अधिकार में है ही नहीं, "
            "चाहे उससे कुछ भी कहा जाए।",
            "Mujhe aisa karne ki ijazat nahi hai. Yeh action is assistant ke adhikar mein hai "
            "hi nahi, chahe usse kuch bhi kaha jaye.",
        ),
        "tool_not_registered": _tri(
            "That is not something this assistant has a tool for.",
            "इसके लिए इस सहायक के पास कोई साधन नहीं है।",
            "Iske liye is assistant ke paas koi tool nahi hai.",
        ),
        "tool_budget_exhausted": _tri(
            "I have used up the checks I am allowed in one turn. Ask me again and I will "
            "start fresh.",
            "एक बार में जितनी जाँच की अनुमति थी वह पूरी हो गई। दोबारा पूछिए, मैं नए सिरे से शुरू करूँगा।",
            "Ek baar mein jitni checks ki ijazat thi wo poori ho gayi. Dobara poochhiye, main "
            "naye sire se shuru karunga.",
        ),
        "tool_unavailable": _tri(
            "That part of the store is not reachable right now.",
            "store का वह हिस्सा अभी उपलब्ध नहीं है।",
            "Dukaan ka wo hissa abhi available nahi hai.",
        ),
        "tool_failed": _tri(
            "That check did not complete, so I will not guess at the answer.",
            "वह जाँच पूरी नहीं हुई, इसलिए मैं अनुमान से जवाब नहीं दूँगा।",
            "Wo check poori nahi hui, isliye main andaaze se jawab nahi dunga.",
        ),
        "injected_instruction": _tri(
            "Some product text tried to give me an instruction. I read product text as "
            "description only, never as a command.",
            "किसी उत्पाद के विवरण ने मुझे निर्देश देने की कोशिश की। मैं उत्पाद का विवरण सिर्फ़ "
            "जानकारी मानता हूँ, आदेश नहीं।",
            "Kisi product ke description ne mujhe instruction dene ki koshish ki. Main product "
            "description sirf jaankari maanta hoon, order nahi.",
        ),
    }
)

_DENIAL_DEFAULT: Final[Mapping[Language, str]] = _tri(
    "I am not able to do that.",
    "मैं ऐसा नहीं कर सकता।",
    "Main aisa nahi kar sakta.",
)


def render_denial(reason_key: str, language: Language, *, tool: str = "") -> str:
    """What a buyer sees when the capability gate refused a tool call.

    ``tool`` is appended as a parenthetical only when given, because naming the tool is
    useful in a console transcript and noise in a conversation. The sentence never
    apologises its way into offering the forbidden thing another way: a denial that
    suggests a workaround is not a denial.
    """
    entry = _DENIAL_TEXT.get(reason_key.casefold(), _DENIAL_DEFAULT)
    sentence = entry.get(language, entry[Language.EN])
    return f"{sentence} ({tool})" if tool else sentence


# --------------------------------------------------------------------------- fallbacks

_FALLBACK: Final[Mapping[Language, str]] = _tri(
    "Part of that answer could not be checked against the store's own data, so I removed "
    "it rather than tell you something I cannot show. Ask me again and I will look it up "
    "fresh.",
    "उस उत्तर का एक हिस्सा store के अपने आँकड़ों से जाँचा नहीं जा सका, इसलिए मैंने उसे हटा "
    "दिया — जो दिखा न सकूँ वह कहूँगा नहीं। दोबारा पूछिए, मैं नए सिरे से देखूँगा।",
    "Us jawab ka ek hissa store ke apne data se check nahi ho paya, isliye maine use hata "
    "diya — jo dikha na sakun wo kahunga nahi. Dobara poochhiye, main naye sire se dekhunga.",
)


def render_fallback(language: Language) -> str:
    """Replacement text when the grounding post-check had to drop what the model wrote.

    Said plainly on purpose. A buyer who is told an answer was withheld can ask again; a
    buyer given a confident wrong number cannot tell that anything happened.
    """
    return _FALLBACK[language]


_REASONING_UNAVAILABLE: Final[Mapping[Language, str]] = _tri(
    "The reasoning layer is unavailable, so this answer comes straight from the store's "
    "own records. Nothing about your cart, your approval or your payment changed.",
    "तर्क करने वाली परत उपलब्ध नहीं है, इसलिए यह उत्तर सीधे store के अपने रिकॉर्ड से आया है। "
    "आपके cart, आपकी मंज़ूरी या आपके भुगतान में कुछ नहीं बदला।",
    "Reasoning layer available nahi hai, isliye yeh jawab seedha store ke apne record se "
    "aaya hai. Aapki cart, aapki approval ya aapke payment mein kuch nahi badla.",
)


def render_reasoning_unavailable(language: Language) -> str:
    """The sentence that leads a turn the model never took part in.

    Distinct from :func:`render_fallback`, and the distinction is the point rather than a
    nicety. ``render_fallback`` says *part of that answer could not be checked* -- it is
    the grounding post-check's voice, spoken about sentences a model did write. This is
    spoken when the model wrote nothing at all: the reasoning layer failed, the platform
    answered from its own records, and the buyer is told which of those two things
    happened. Using the wrong one would tell the buyer something false about the turn
    they just had.

    It names no error and offers no apology. The answer that follows it is correct and
    grounded, because it never came from the model in the first place.
    """
    return _REASONING_UNAVAILABLE[language]


_UNVERIFIED: Final[Mapping[Language, str]] = _tri(
    "You are back from Razorpay. The platform has not verified the outcome yet — returning "
    "to this page is not evidence that money moved. Razorpay's own record decides, and I "
    "will tell you the moment it is verified. Nothing is fulfilled before then.",
    "आप Razorpay से लौट आए हैं। मंच ने अभी नतीजे की पुष्टि नहीं की है — इस पृष्ठ पर लौट आना "
    "इस बात का प्रमाण नहीं है कि राशि गई। Razorpay का अपना रिकॉर्ड ही तय करता है, और पुष्टि "
    "होते ही मैं आपको बताऊँगा। उससे पहले कुछ भी पूरा नहीं होगा।",
    "Aap Razorpay se wapas aa gaye hain. Platform ne abhi nateeje ki pushti nahi ki hai — is "
    "page par wapas aa jana iska proof nahi hai ki paisa gaya. Razorpay ka apna record hi tay "
    "karta hai, aur pushti hote hi main aapko bataunga. Usse pehle kuch bhi poora nahi hoga.",
)

_UNVERIFIED_AMOUNT: Final[Mapping[Language, str]] = _tri(
    "The amount under attempt is {amount}.",
    "जिस राशि का प्रयास हुआ वह {amount} है।",
    "Jis amount ka attempt hua wo {amount} hai.",
)


def render_unverified(
    language: Language, *, amount_minor: int | None = None, currency: str = "INR"
) -> str:
    """What the buyer sees on returning from Razorpay, before the server has verified.

    ADR 0003 D8: the browser callback is never capture evidence. This sentence therefore
    never says the money moved -- it names what is not yet known and who decides. The
    optional amount comes from a structured read (the approval card or the attempt), never
    from conversation state, and is formatted, never computed.
    """
    text = _UNVERIFIED[language]
    if amount_minor is None:
        return text
    amount = display_minor(amount_minor, currency)
    return f"{text} {_UNVERIFIED_AMOUNT[language].format(amount=amount)}"

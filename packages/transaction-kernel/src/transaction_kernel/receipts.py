"""Policy-at-Sale Receipt, specification 10.2.1.

Why this module exists
----------------------
A sale is governed by the merchant's rules *as they stood when the buyer agreed to them*.
Merchants edit those rules: a refund window is shortened, a restocking fee appears, a
substitution rule is narrowed. Without a frozen record, the only rule set a refund six
weeks later can consult is the current one, and the buyer silently loses the terms they
were shown. That is not a display bug. It is a retroactive edit to a concluded agreement,
and it is indistinguishable from fraud in an audit.

So when a checkout first enters ``APPROVAL_REQUIRED`` the kernel freezes every applicable
merchant-policy id and immutable version, the cancellation, refund, substitution,
delivery, discount and fulfilment terms, the ``applies_to`` targets, the buyer-visible
references and the tax and rounding policy versions into one JSONB document, hashes it
with :func:`commerce_domain.canonical_hash`, and writes ``policy_receipt_id`` and
``policy_receipt_hash`` onto the checkout version.

The binding is mechanical, not narrative
----------------------------------------
Checkout version 7 bound to receipt v12 cannot later be recombined with receipt v13:

* the checkout version stores the receipt's *hash* as well as its id, so re-pointing the
  id at another row leaves a hash that no longer matches;
* the receipt independently names its own tenant, merchant, checkout id, checkout version
  and checkout content hash, so rewriting both the id *and* the hash still leaves a
  document that does not describe this checkout;
* the receipt hash is recomputed from the stored JSONB on every verification, so editing
  a stored term is detected even when the stored hash is edited to match.

Honest limit: this module verifies the two copies it owns -- the receipt row and the
checkout version. Specification 10.2.1 requires ``policy_receipt_hash`` to be copied onto
the approval and onto the resulting order as well, and it is included in the signed or
hashed approval material. Those copies are owned by the approval and order modules; an
edit that forges all of them still has to survive the append-only audit chain of
specification 26.2. The point of the design is that no single UPDATE can rewrite a sale's
terms.

Nothing here calls a model. Nothing here reads the merchant's *current* policy:
:func:`policy_for_order` deliberately has no code path that could reach one.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from commerce_domain import CanonicalizationError, DomainError, Money, canonical_hash, uuid7
from platform_db import CheckoutVersion, PolicyAtSaleReceipt, require_tenant
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .contracts import CheckoutRef
from .recovery import RecoveryCode

#: Version tag inside the hashed content. A future receipt shape gets a new tag rather
#: than a silent change of meaning, because stored hashes must stay reproducible forever.
RECEIPT_SCHEMA: Final = "policy_at_sale_receipt/1"

#: The only checkout status at which a receipt may be issued, specification 10.2.1.
#: Issuing earlier would freeze terms the buyer has not been shown; issuing later would
#: mean an approval was requested with no recorded terms at all.
ISSUE_AT_STATUS: Final = "APPROVAL_REQUIRED"

#: ``applies_to`` target for a term that governs the whole order.
TARGET_ORDER: Final = "ORDER"


def item_target(line_id: str) -> str:
    """``applies_to`` target for one checkout line. Use this rather than an ad-hoc string.

    Guarantees a single spelling of a line target across merchants, so that a later
    resolution can match targets by equality instead of by interpretation.
    """
    if not line_id:
        raise ReceiptContentError("item_target requires a non-empty line id")
    return f"ITEM:{line_id}"


# --------------------------------------------------------------------------- errors


class ReceiptError(DomainError):
    """A Policy-at-Sale Receipt could not be issued, bound or trusted."""


class ReceiptContentError(ReceiptError):
    """The draft is not a complete, canonicalizable description of the governing rules."""


class ReceiptImmutableError(ReceiptError):
    """A receipt already governs this sale. Receipts are written once and never edited."""


class ReceiptBindingError(ReceiptError):
    """The receipt cannot be bound to the checkout version the draft names."""


# ------------------------------------------------------------------- content value types


class PolicyKind(StrEnum):
    """The families of merchant rule a sale is governed by, specification 10.2.1."""

    CANCELLATION = "CANCELLATION"
    REFUND = "REFUND"
    SUBSTITUTION = "SUBSTITUTION"
    DELIVERY = "DELIVERY"
    DISCOUNT = "DISCOUNT"
    FULFILMENT = "FULFILMENT"


#: Every kind must be present in every receipt, even when the merchant's answer is "no".
#: An omitted kind is the whole failure mode this module exists to prevent: at resolution
#: time a silent gap gets filled from the merchant's *current* policy, which is exactly
#: the retroactive change the receipt is supposed to make impossible. A merchant with no
#: substitution programme records ``{"allowed": False}`` and says so on the record.
REQUIRED_POLICY_KINDS: Final[frozenset[PolicyKind]] = frozenset(PolicyKind)


def _jsonify(value: object, path: str) -> Any:
    """Convert one term value into the integer-only JSON profile, or refuse.

    Refuses float, Decimal, datetime, date and bytes. Accepts :class:`Money`, expanding it
    to ``{"currency", "minor"}`` so a stored cap stays an integer that a later resolution
    can compare, rather than a "395.00" string somebody has to re-parse.
    """
    if isinstance(value, Money):
        return {"currency": value.currency, "minor": value.minor}
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | Decimal):
        raise ReceiptContentError(
            f"non-integer number at {path}: receipt terms are integers only. Express money "
            "as commerce_domain.Money and rates as integer basis points (10% -> 1000). A "
            "float cannot be canonicalized, so a receipt holding one could not be hashed "
            "or re-verified."
        )
    if isinstance(value, datetime | date):
        raise ReceiptContentError(
            f"{type(value).__name__} at {path}: record an integer epoch or an explicit "
            "ISO-8601 string. A datetime's bytes depend on tzinfo and repr, so the receipt "
            "hash would not be reproducible."
        )
    if isinstance(value, bytes | bytearray):
        raise ReceiptContentError(f"raw bytes at {path} cannot be recorded in a receipt")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReceiptContentError(f"non-string term key {key!r} at {path}")
            out[key] = _jsonify(item, f"{path}.{key}")
        return out
    if isinstance(value, Sequence):
        return [_jsonify(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ReceiptContentError(f"cannot record {type(value).__name__} at {path} in a receipt")


def _clean_targets(targets: Sequence[str], path: str) -> tuple[str, ...]:
    """Deduplicate and sort ``applies_to`` targets so ordering cannot change the hash."""
    if not targets:
        raise ReceiptContentError(
            f"{path}: applies_to must name at least one target; use TARGET_ORDER for an "
            "order-wide term. A term with no target governs nothing."
        )
    for target in targets:
        if not isinstance(target, str) or not target:
            raise ReceiptContentError(f"{path}: applies_to entries must be non-empty strings")
    return tuple(sorted(set(targets)))


@dataclass(frozen=True, slots=True)
class MerchantPolicy:
    """One merchant rule document at one immutable version, as it applied to this sale.

    ``policy_version`` is the merchant's own immutable version number. It is recorded so a
    dispute can be argued against the exact document the buyer was shown, and so a later
    edit produces a different version rather than mutating this one.
    """

    kind: PolicyKind
    policy_id: str
    policy_version: int
    terms: Mapping[str, Any]
    applies_to: tuple[str, ...] = (TARGET_ORDER,)
    document_ref: str | None = None
    document_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PolicyKind):
            raise ReceiptContentError(f"kind must be a PolicyKind, got {self.kind!r}")
        if not self.policy_id:
            raise ReceiptContentError("policy_id must be a non-empty merchant policy id")
        if isinstance(self.policy_version, bool) or not isinstance(self.policy_version, int):
            raise ReceiptContentError(
                f"policy_version for {self.policy_id!r} must be an int, "
                f"got {type(self.policy_version).__name__}"
            )
        if self.policy_version < 1:
            raise ReceiptContentError(
                f"policy_version for {self.policy_id!r} must be >= 1, got {self.policy_version}"
            )
        if not isinstance(self.terms, Mapping) or not self.terms:
            raise ReceiptContentError(
                f"{self.policy_id!r}: terms must be a non-empty mapping. An empty term set "
                "records nothing, and a later resolution would have to guess."
            )
        # Canonicalize eagerly: a term that cannot be hashed must fail while the merchant
        # is configuring it, not at 02:00 when a refund is being evaluated.
        object.__setattr__(self, "terms", _jsonify(self.terms, f"terms[{self.policy_id}]"))
        object.__setattr__(
            self, "applies_to", _clean_targets(self.applies_to, f"policy[{self.policy_id}]")
        )

    def as_content(self) -> dict[str, Any]:
        """This policy's contribution to the hashed receipt content."""
        return {
            "kind": str(self.kind),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "applies_to": list(self.applies_to),
            "terms": dict(self.terms),
            "document_ref": self.document_ref,
            "document_hash": self.document_hash,
        }


@dataclass(frozen=True, slots=True)
class BuyerVisibleRef:
    """A link or text reference the buyer could actually read at the time of sale.

    ``text_hash`` covers inline terms that have no stable URL: hashing what was rendered
    is the only way to later prove which words the buyer saw.
    """

    label: str
    uri: str | None = None
    text_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise ReceiptContentError("a buyer-visible reference needs a label")
        if not self.uri and not self.text_hash:
            raise ReceiptContentError(
                f"{self.label!r}: a buyer-visible reference needs a uri or a text_hash. "
                "Without one there is no evidence of what the buyer was shown."
            )

    def as_content(self) -> dict[str, Any]:
        return {"label": self.label, "uri": self.uri, "text_hash": self.text_hash}


@dataclass(frozen=True, slots=True)
class ReceiptDraft:
    """Everything that must be frozen for one sale. Validated on construction.

    A draft is not yet a receipt: it carries no timestamp and no hash, because both come
    from the database at issue time.
    """

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    checkout_hash: str
    policies: tuple[MerchantPolicy, ...]
    tax_policy_version: str
    rounding_policy_version: str
    buyer_visible_refs: tuple[BuyerVisibleRef, ...]
    correlation_id: uuid.UUID
    store_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if isinstance(self.checkout_version, bool) or not isinstance(self.checkout_version, int):
            raise ReceiptContentError("checkout_version must be an int")
        if self.checkout_version < 1:
            raise ReceiptContentError(f"checkout_version must be >= 1, got {self.checkout_version}")
        if not self.checkout_hash:
            raise ReceiptContentError(
                "checkout_hash is required: the receipt names the exact checkout body it "
                "governs, so that a receipt cannot be re-pointed at different contents"
            )
        if not self.tax_policy_version or not self.rounding_policy_version:
            raise ReceiptContentError(
                "tax_policy_version and rounding_policy_version are required. A one-paisa "
                "rounding change is a real change to what the buyer owes, and admission "
                "fails on one paisa."
            )
        if not self.buyer_visible_refs:
            raise ReceiptContentError(
                "at least one buyer-visible policy reference is required: a receipt that "
                "cannot show what the buyer was shown is not evidence"
            )
        self._validate_policies()
        # Sort so that two callers assembling the same rule set in different orders
        # produce identical bytes. Without this, a reordered list would read as a policy
        # change and break a binding that is in fact intact.
        object.__setattr__(
            self,
            "policies",
            tuple(sorted(self.policies, key=lambda p: (str(p.kind), p.policy_id))),
        )
        object.__setattr__(
            self,
            "buyer_visible_refs",
            tuple(
                sorted(
                    dict.fromkeys(self.buyer_visible_refs),
                    key=lambda r: (r.label, r.uri or "", r.text_hash or ""),
                )
            ),
        )

    def _validate_policies(self) -> None:
        if not self.policies:
            raise ReceiptContentError("a receipt must record at least one merchant policy")
        seen_pairs: set[tuple[str, str]] = set()
        versions: dict[str, int] = {}
        for policy in self.policies:
            pair = (str(policy.kind), policy.policy_id)
            if pair in seen_pairs:
                raise ReceiptContentError(
                    f"policy {policy.policy_id!r} appears twice for {policy.kind}"
                )
            seen_pairs.add(pair)
            # One document may govern several kinds, but only at one version: two versions
            # of one document governing one sale is a contradiction, not a configuration.
            previous = versions.setdefault(policy.policy_id, policy.policy_version)
            if previous != policy.policy_version:
                raise ReceiptContentError(
                    f"policy {policy.policy_id!r} appears at versions {previous} and "
                    f"{policy.policy_version}; one sale is governed by one version"
                )
        covered = {p.kind for p in self.policies}
        missing = REQUIRED_POLICY_KINDS - covered
        if missing:
            raise ReceiptContentError(
                "receipt does not cover " + ", ".join(sorted(str(k) for k in missing)) + ". "
                "Record an explicit term (for example {'allowed': False}) rather than "
                "omitting the kind: an omitted kind is filled in from current policy later, "
                "which is the retroactive change this receipt exists to prevent."
            )


def build_receipt_content(draft: ReceiptDraft, *, created_at_ms: int) -> dict[str, Any]:
    """The exact document that gets hashed and stored. Pure: no I/O, no clock.

    ``created_at_ms`` must come from the database clock (see :func:`database_now_ms`).
    It is inside the hashed content on purpose, so that the recorded moment of freezing
    cannot be moved without breaking the hash.

    Guarantees: the same draft and timestamp always produce byte-identical canonical JSON,
    regardless of the order the caller assembled the policies in.
    """
    if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int):
        raise ReceiptContentError("created_at_ms must be an integer epoch in milliseconds")
    return {
        "schema": RECEIPT_SCHEMA,
        "tenant_id": str(draft.tenant_id),
        "merchant_id": str(draft.merchant_id),
        "store_id": None if draft.store_id is None else str(draft.store_id),
        "checkout_id": str(draft.checkout_id),
        "checkout_version": draft.checkout_version,
        "checkout_hash": draft.checkout_hash,
        "policies": [policy.as_content() for policy in draft.policies],
        "tax_policy_version": draft.tax_policy_version,
        "rounding_policy_version": draft.rounding_policy_version,
        "buyer_visible_refs": [ref.as_content() for ref in draft.buyer_visible_refs],
        "created_at_ms": created_at_ms,
        "correlation_id": str(draft.correlation_id),
    }


@dataclass(frozen=True, slots=True)
class IssuedReceipt:
    """A receipt that now exists in the database and is bound to its checkout version."""

    receipt_id: uuid.UUID
    receipt_hash: str
    checkout: CheckoutRef
    created_at_ms: int
    content: Mapping[str, Any]


# ------------------------------------------------------------------------- database clock

# now() is the transaction timestamp, so the value embedded in the content is identical to
# the row's created_at default. An application clock is never consulted: a pod skewed by
# a minute must not be able to stamp a receipt with a time the database never saw.
_DB_NOW_MS = text("SELECT (EXTRACT(EPOCH FROM now()) * 1000)::bigint")


def database_now_ms(session: Session) -> int:
    """Current transaction time from the database clock, in epoch milliseconds.

    Refuses to fall back to a local clock: there is no fallback path. Two calls inside one
    transaction return the same value, because ``now()`` is the transaction timestamp.
    """
    return int(session.execute(_DB_NOW_MS).scalar_one())


# ------------------------------------------------------------------------------- issuance


def issue_receipt(session: Session, draft: ReceiptDraft) -> IssuedReceipt:
    """Freeze the governing rules for one sale and bind them to its checkout version.

    Guarantees, all inside the caller's transaction so they commit or vanish together:

    * the receipt row is written once, with ``receipt_hash = canonical_hash(content)``;
    * ``policy_receipt_id`` and ``policy_receipt_hash`` are written onto the checkout
      version, which is what makes the binding mechanical rather than narrative;
    * the checkout version row is locked ``FOR UPDATE`` first, so two concurrent issuers
      serialize and the second one sees the first one's receipt.

    Refuses to:

    * issue for a tenant other than the one bound to this transaction;
    * issue for a checkout version that does not exist, is not at ``APPROVAL_REQUIRED``,
      belongs to another merchant, or whose stored ``content_hash`` differs from the
      draft's -- a receipt that names a body the checkout does not have is not a binding;
    * issue a second receipt for a checkout version that already has one. A merchant who
      changed policy does not get to re-freeze a sale that is already frozen; that is the
      retroactive edit this whole module prevents.

    Does not commit. The caller owns the transaction.
    """
    bound_tenant = require_tenant(session)
    if bound_tenant != draft.tenant_id:
        raise ReceiptBindingError(
            f"draft tenant {draft.tenant_id} is not the tenant bound to this transaction "
            f"({bound_tenant}); row-level security would reject the write with an empty "
            "result rather than an error"
        )

    version_row = session.execute(
        select(CheckoutVersion)
        .where(
            CheckoutVersion.checkout_id == draft.checkout_id,
            CheckoutVersion.version == draft.checkout_version,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if version_row is None:
        raise ReceiptBindingError(
            f"checkout {draft.checkout_id} version {draft.checkout_version} does not exist "
            "for this tenant"
        )
    if version_row.merchant_id != draft.merchant_id:
        raise ReceiptBindingError(
            f"draft names merchant {draft.merchant_id} but the checkout version belongs to "
            f"{version_row.merchant_id}"
        )
    if version_row.content_hash != draft.checkout_hash:
        raise ReceiptBindingError(
            "draft checkout_hash does not match the stored checkout version hash; the "
            "receipt would name a checkout body that this version does not have"
        )
    if version_row.status != ISSUE_AT_STATUS:
        raise ReceiptBindingError(
            f"a receipt is issued when a checkout first enters {ISSUE_AT_STATUS}; this "
            f"version is {version_row.status}"
        )
    if version_row.policy_receipt_id is not None:
        raise ReceiptImmutableError(
            f"checkout {draft.checkout_id} version {draft.checkout_version} is already bound "
            f"to receipt {version_row.policy_receipt_id}; a receipt is never replaced"
        )
    # Not redundant with the check above, and deliberately a column select rather than an
    # entity one. Two ways the attribute read above can miss a receipt this query catches:
    # an operator who cleared policy_receipt_id to "unfreeze" a sale, and a caller whose
    # session had already loaded this CheckoutVersion -- SQLAlchemy returns the
    # identity-mapped object with its previously loaded attributes, so the values are the
    # caller's older view even though FOR UPDATE re-read the row. A column select builds no
    # entity, so it always reports what the database holds now.
    existing = session.execute(
        select(PolicyAtSaleReceipt.id).where(
            PolicyAtSaleReceipt.checkout_id == draft.checkout_id,
            PolicyAtSaleReceipt.checkout_version == draft.checkout_version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ReceiptImmutableError(
            f"receipt {existing} already governs checkout {draft.checkout_id} version "
            f"{draft.checkout_version}"
        )

    created_at_ms = database_now_ms(session)
    content = build_receipt_content(draft, created_at_ms=created_at_ms)
    receipt_hash = canonical_hash(content)
    receipt_id = uuid7()

    session.add(
        PolicyAtSaleReceipt(
            id=receipt_id,
            tenant_id=draft.tenant_id,
            merchant_id=draft.merchant_id,
            checkout_id=draft.checkout_id,
            checkout_version=draft.checkout_version,
            content=content,
            receipt_hash=receipt_hash,
        )
    )
    # Flush the receipt before the binding: checkout_versions.policy_receipt_id carries a
    # foreign key to this row, and the unit of work does not order an INSERT of a new
    # object ahead of an UPDATE of an already-loaded one on its own.
    session.flush()

    # Both halves of the binding are written here. The row is locked FOR UPDATE and was
    # observed unbound above, so no conditional UPDATE is needed: the lock is the
    # serialization point, the fresh receipt lookup above is what catches a binding this
    # session's identity map cannot see, and the unique constraint on
    # (tenant_id, checkout_id, checkout_version) is the backstop under both.
    version_row.policy_receipt_id = receipt_id
    version_row.policy_receipt_hash = receipt_hash
    session.flush()

    return IssuedReceipt(
        receipt_id=receipt_id,
        receipt_hash=receipt_hash,
        checkout=CheckoutRef(
            checkout_id=draft.checkout_id,
            version=draft.checkout_version,
            content_hash=draft.checkout_hash,
        ),
        created_at_ms=created_at_ms,
        # Defensive rather than load-bearing today: `content` is also the dict held by the
        # ORM row, and SQLAlchemy's identity map is weak, so that row is normally collected
        # the moment this flush finishes. Copying anyway costs nothing and means the
        # guarantee survives a future MutableDict, a caching session or a repository that
        # keeps the row alive -- any of which would turn a caller's edit of this dict into
        # an edit of the receipt it had just frozen.
        content=copy.deepcopy(content),
    )


# --------------------------------------------------------------------------- verification


class BindingReason(StrEnum):
    """Stable reason keys for a binding verdict. Closed set, never free text.

    A :class:`RecoveryCode` says what the caller may do; the reason key says which of the
    binding's independent checks failed, so an operator reading an audit trail can tell a
    stale caller from a swapped receipt from an edited one.
    """

    OK = "OK"
    CHECKOUT_VERSION_MISSING = "CHECKOUT_VERSION_MISSING"
    CHECKOUT_HASH_MISMATCH = "CHECKOUT_HASH_MISMATCH"
    RECEIPT_NOT_BOUND = "RECEIPT_NOT_BOUND"
    RECEIPT_MISSING = "RECEIPT_MISSING"
    RECEIPT_CONTENT_TAMPERED = "RECEIPT_CONTENT_TAMPERED"
    RECEIPT_SWAPPED = "RECEIPT_SWAPPED"
    RECEIPT_FOREIGN = "RECEIPT_FOREIGN"
    RECEIPT_CHECKOUT_HASH_MISMATCH = "RECEIPT_CHECKOUT_HASH_MISMATCH"


@dataclass(frozen=True, slots=True)
class BindingVerdict:
    """The result of re-deriving the checkout/receipt binding from stored rows."""

    ok: bool
    code: RecoveryCode
    reason: BindingReason
    receipt_id: uuid.UUID | None = None
    receipt_hash: str | None = None

    def __post_init__(self) -> None:
        if self.ok != (self.reason is BindingReason.OK):
            raise ValueError("ok and BindingReason.OK must agree")
        if self.ok != (self.code is RecoveryCode.OK):
            raise ValueError("ok and RecoveryCode.OK must agree")


def _fail(reason: BindingReason, code: RecoveryCode, **extra: Any) -> BindingVerdict:
    return BindingVerdict(ok=False, code=code, reason=reason, **extra)


def _verify(
    session: Session, checkout: CheckoutRef
) -> tuple[BindingVerdict, PolicyAtSaleReceipt | None]:
    """Shared implementation. Returns the receipt only when the binding fully verifies."""
    version_row = session.execute(
        select(CheckoutVersion).where(
            CheckoutVersion.checkout_id == checkout.checkout_id,
            CheckoutVersion.version == checkout.version,
        )
    ).scalar_one_or_none()
    if version_row is None:
        return _fail(
            BindingReason.CHECKOUT_VERSION_MISSING, RecoveryCode.HUMAN_REVIEW_REQUIRED
        ), None

    # The caller's own view of the checkout must match the stored one. A (checkout, version)
    # pair has exactly one content hash forever, so a mismatch means the caller is holding a
    # superseded version -- it must re-read and, if material, seek approval again.
    if version_row.content_hash != checkout.content_hash:
        return _fail(BindingReason.CHECKOUT_HASH_MISMATCH, RecoveryCode.STALE_CHECKOUT), None

    if version_row.policy_receipt_id is None or version_row.policy_receipt_hash is None:
        return _fail(BindingReason.RECEIPT_NOT_BOUND, RecoveryCode.HUMAN_REVIEW_REQUIRED), None

    receipt = session.get(PolicyAtSaleReceipt, version_row.policy_receipt_id)
    if receipt is None:
        return _fail(BindingReason.RECEIPT_MISSING, RecoveryCode.HUMAN_REVIEW_REQUIRED), None

    # Recompute rather than trust the stored hash: an edited term with an un-edited hash is
    # the cheapest possible forgery, and this is the check that catches it.
    try:
        recomputed = canonical_hash(receipt.content)
    except CanonicalizationError:
        # Content that will not canonicalize cannot have produced the stored hash, so it
        # was not written by issue_receipt.
        return (
            _fail(
                BindingReason.RECEIPT_CONTENT_TAMPERED,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
            ),
            None,
        )
    if recomputed != receipt.receipt_hash:
        return (
            _fail(
                BindingReason.RECEIPT_CONTENT_TAMPERED,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
            ),
            None,
        )

    # A stored document that is not a JSON object cannot have come from issue_receipt,
    # which always writes one. The check above does not catch it: canonical_hash accepts a
    # bare array or string, so an attacker with UPDATE can store `[]` alongside its own
    # hash and pass recomputation. Without this guard the `.get` calls below raise
    # AttributeError, which breaks this function's contract -- a broken row must reach the
    # admission transaction as a verdict carrying a recovery code, never as an unhandled
    # exception that rolls it back with nothing to tell the buyer.
    content = receipt.content
    if not isinstance(content, Mapping):
        return (
            _fail(
                BindingReason.RECEIPT_CONTENT_TAMPERED,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
            ),
            None,
        )

    # Second copy of the binding: re-pointing policy_receipt_id at another receipt leaves
    # the checkout version's policy_receipt_hash describing the receipt it lost.
    if receipt.receipt_hash != version_row.policy_receipt_hash:
        return (
            _fail(
                BindingReason.RECEIPT_SWAPPED,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
                receipt_hash=receipt.receipt_hash,
            ),
            None,
        )

    # Third copy: the receipt names its own subject, so swapping id *and* hash together
    # still yields a document that does not describe this checkout.
    if (
        receipt.checkout_id != checkout.checkout_id
        or receipt.checkout_version != checkout.version
        or content.get("checkout_id") != str(checkout.checkout_id)
        or content.get("checkout_version") != checkout.version
        or content.get("tenant_id") != str(version_row.tenant_id)
        or content.get("merchant_id") != str(version_row.merchant_id)
    ):
        return (
            _fail(
                BindingReason.RECEIPT_FOREIGN,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
                receipt_hash=receipt.receipt_hash,
            ),
            None,
        )

    if content.get("checkout_hash") != version_row.content_hash:
        return (
            _fail(
                BindingReason.RECEIPT_CHECKOUT_HASH_MISMATCH,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                receipt_id=receipt.id,
                receipt_hash=receipt.receipt_hash,
            ),
            None,
        )

    return (
        BindingVerdict(
            ok=True,
            code=RecoveryCode.OK,
            reason=BindingReason.OK,
            receipt_id=receipt.id,
            receipt_hash=receipt.receipt_hash,
        ),
        receipt,
    )


def verify_binding(session: Session, checkout: CheckoutRef) -> BindingVerdict:
    """Re-derive the checkout/receipt binding from stored rows. Never raises on bad data.

    Guarantees that a verdict of ``ok`` means all of the following held at read time:
    the checkout version exists and matches the caller's hash; a receipt is bound; the
    receipt's hash is reproducible from its own stored content; that hash equals the copy
    on the checkout version; and the receipt names this tenant, merchant, checkout,
    version and checkout hash.

    Refuses to repair anything. A broken binding is evidence of tampering or of a bug in a
    writer, and silently re-binding would destroy the only trace of it.
    """
    verdict, _ = _verify(session, checkout)
    return verdict


# ---------------------------------------------------------------------------- resolution


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    """The rules that govern one sale, as frozen at sale time.

    ``content`` is a deep copy. The stored JSONB is a live ORM attribute; handing it out
    would let a caller mutate the receipt and have the change flushed at commit.
    """

    code: RecoveryCode
    reason: BindingReason
    receipt_id: uuid.UUID | None = None
    receipt_hash: str | None = None
    content: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.code is RecoveryCode.OK

    @property
    def policies(self) -> tuple[Mapping[str, Any], ...]:
        """Every merchant policy this sale is governed by, as recorded."""
        if self.content is None:
            raise ReceiptError(f"no at-sale policy is available: {self.reason}")
        entries: Sequence[Mapping[str, Any]] = self.content["policies"]
        return tuple(entries)

    def policy_for(self, kind: PolicyKind) -> Mapping[str, Any]:
        """The single recorded policy of ``kind``. Raises if absent or ambiguous."""
        matches = [p for p in self.policies if p["kind"] == str(kind)]
        if not matches:
            raise ReceiptError(f"receipt {self.receipt_id} records no {kind} policy")
        if len(matches) > 1:
            raise ReceiptError(
                f"receipt {self.receipt_id} records {len(matches)} {kind} policies; the "
                "caller must choose by applies_to target rather than assume one"
            )
        return matches[0]

    def terms_for(self, kind: PolicyKind) -> Mapping[str, Any]:
        """The at-sale terms of ``kind``. Never the merchant's current terms."""
        terms: Mapping[str, Any] = self.policy_for(kind)["terms"]
        return terms


def policy_for_order(session: Session, checkout: CheckoutRef) -> ResolvedPolicy:
    """Return the AT-SALE policy for a checkout or order. Never the current policy.

    This is the resolver the Resolution Service uses. A merchant who tightened their refund
    rule yesterday must not retroactively narrow a sale made last week, so the only source
    consulted here is the immutable receipt frozen when the buyer approved. There is no
    parameter, flag or fallback that reaches a live merchant-policy table.

    Refuses to return terms when the binding does not verify: it returns the failing
    :class:`RecoveryCode` and ``content is None``. Returning a receipt whose binding is
    broken would let a forged or mismatched document govern a real refund, which is worse
    than returning nothing.
    """
    verdict, receipt = _verify(session, checkout)
    if receipt is None:
        return ResolvedPolicy(
            code=verdict.code,
            reason=verdict.reason,
            receipt_id=verdict.receipt_id,
            receipt_hash=verdict.receipt_hash,
        )
    return ResolvedPolicy(
        code=RecoveryCode.OK,
        reason=BindingReason.OK,
        receipt_id=receipt.id,
        receipt_hash=receipt.receipt_hash,
        content=copy.deepcopy(receipt.content),
    )


def load_receipt(session: Session, receipt_id: uuid.UUID) -> IssuedReceipt | None:
    """Read one receipt by id, verifying its content still hashes to its stored hash.

    Returns ``None`` when no such receipt is visible to the bound tenant. Raises
    :class:`ReceiptError` when the stored content no longer reproduces the stored hash,
    because a caller asking for a receipt by id has no other way to learn that.
    """
    receipt = session.get(PolicyAtSaleReceipt, receipt_id)
    if receipt is None:
        return None
    try:
        recomputed = canonical_hash(receipt.content)
    except CanonicalizationError as exc:
        raise ReceiptError(f"receipt {receipt_id} content cannot be canonicalized") from exc
    if recomputed != receipt.receipt_hash:
        raise ReceiptError(
            f"receipt {receipt_id} content does not reproduce its stored hash; the row has "
            "been edited since it was issued"
        )
    # canonical_hash accepts a bare array or string, so a matching hash does not by itself
    # prove the row holds a receipt document. Checking the shape keeps the documented
    # failure a ReceiptError rather than a TypeError or KeyError from the reads below.
    stored = receipt.content
    if not isinstance(stored, Mapping) or not {"checkout_hash", "created_at_ms"} <= stored.keys():
        raise ReceiptError(
            f"receipt {receipt_id} content is not a receipt document; the row has been "
            "edited since it was issued"
        )
    content = copy.deepcopy(stored)
    return IssuedReceipt(
        receipt_id=receipt.id,
        receipt_hash=receipt.receipt_hash,
        checkout=CheckoutRef(
            checkout_id=receipt.checkout_id,
            version=receipt.checkout_version,
            content_hash=str(content["checkout_hash"]),
        ),
        created_at_ms=int(content["created_at_ms"]),
        content=content,
    )

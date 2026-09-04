"""Adapter errors.

These signal a *programming or configuration* fault: the caller asked for something the
provider contract cannot express, or the process is misconfigured. They are not business
outcomes. A business outcome is always a ``RecoveryCode`` carried on a result object,
never an exception, because a payment that failed at the provider is a normal event that
the platform must record -- while a receipt longer than the provider accepts is a bug
that must stop the request before any money moves.
"""

from __future__ import annotations

from commerce_domain import DomainError

__all__ = [
    "ConfigurationError",
    "RazorpayAdapterError",
    "RefundNotPermittedError",
    "RequestConstructionError",
    "SignatureMismatchError",
    "UnmappableEventError",
]


class RazorpayAdapterError(DomainError):
    """Base for every deterministic failure raised by the Razorpay adapter."""


class ConfigurationError(RazorpayAdapterError):
    """Credentials or profile settings that must not be allowed to start the process.

    Raised at load time rather than at first use: a live key discovered on the first
    real checkout has already had the chance to move somebody's money.
    """


class RequestConstructionError(RazorpayAdapterError):
    """A request that violates a provider constraint and must never be sent.

    Specification 12.5: provider constraints are enforced in the adapter, so the platform
    core never has to know that Razorpay caps a receipt at 40 characters.
    """


class RefundNotPermittedError(RazorpayAdapterError):
    """A refund was requested from a state where issuing one could refund twice.

    The specific state this guards is ``REFUND_UNKNOWN`` (specification 10.6): the
    provider outcome is genuinely unknown, a refund may already exist, and the only
    lawful next step is reconciliation against authoritative identifiers.
    """


class SignatureMismatchError(RazorpayAdapterError):
    """A signature did not verify.

    Raised only by the ``require_*`` helpers in :mod:`.signatures`. The ``verify_*``
    functions return ``False`` instead, so that a caller handling an untrusted public
    endpoint can answer with a flat rejection without an exception in the hot path.

    Carries no detail about *why* it failed. A caller that could distinguish "malformed
    hex" from "wrong HMAC" would be an oracle for anyone probing the endpoint.
    """


class UnmappableEventError(RazorpayAdapterError):
    """An event whose local meaning cannot be determined without more facts.

    Raised rather than guessed. The motivating case is ``refund.processed``, which means
    ``REFUNDED`` for a full refund and ``PARTIALLY_REFUNDED`` for a partial one. Guessing
    ``REFUNDED`` puts the attempt in a terminal state and permanently strands the money
    the buyer is still owed.
    """

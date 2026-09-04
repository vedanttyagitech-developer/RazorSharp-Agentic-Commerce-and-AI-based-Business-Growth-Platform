"""Domain errors. Deterministic, never raised from a model call."""


class DomainError(Exception):
    """Base for every deterministic domain failure."""


class MoneyError(DomainError):
    """Invalid monetary construction or operation."""


class CurrencyMismatchError(MoneyError):
    """Two amounts in different currencies were combined."""


class CanonicalizationError(DomainError):
    """A value cannot be canonicalized under RFC 8785 in this profile."""

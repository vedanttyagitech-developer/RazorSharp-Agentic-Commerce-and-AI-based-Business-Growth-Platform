"""Money as integer minor units.

Spec invariant: financial amounts are integer minor units plus an ISO 4217 currency.
A float never enters this module. There is no ``from_float``; there is no ``__truediv__``
that can silently produce a fraction of a paisa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ISO 4217 minor-unit exponents for the currencies this platform accepts.
# Extend deliberately; an unknown currency is an error, never a guess.
_EXPONENTS: Final[dict[str, int]] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "SGD": 2,
    "AED": 2,
    "JPY": 0,
}


def exponent_for(currency: str) -> int:
    """Minor-unit exponent for an accepted ISO 4217 code."""
    from .errors import MoneyError

    try:
        return _EXPONENTS[currency]
    except KeyError:
        raise MoneyError(f"unsupported currency {currency!r}") from None


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact amount. ``minor`` is the whole number of minor units (paise for INR)."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        from .errors import MoneyError

        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise MoneyError(f"minor must be int, got {type(self.minor).__name__}")
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise MoneyError(f"currency must be a 3-letter code, got {self.currency!r}")
        if self.currency != self.currency.upper():
            raise MoneyError(f"currency must be uppercase, got {self.currency!r}")
        exponent_for(self.currency)  # rejects unsupported currencies

    # ---- construction -------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @classmethod
    def parse(cls, text: str, currency: str) -> Money:
        """Parse a decimal string exactly. ``Money.parse("395.00", "INR") == 39500 paise``.

        Uses Decimal, never float. Rejects more precision than the currency allows,
        because silently rounding a buyer's money is how a one-paisa admission failure
        becomes a mystery.
        """
        from decimal import Decimal, InvalidOperation

        from .errors import MoneyError

        exp = exponent_for(currency)
        try:
            dec = Decimal(text)
        except InvalidOperation:
            raise MoneyError(f"not a decimal amount: {text!r}") from None
        scaled = dec.scaleb(exp)
        if scaled != scaled.to_integral_value():
            raise MoneyError(
                f"{text!r} has more precision than {currency} allows ({exp} minor digits)"
            )
        return cls(int(scaled), currency)

    # ---- arithmetic ---------------------------------------------------

    def _same(self, other: Money) -> None:
        from .errors import CurrencyMismatchError

        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __mul__(self, qty: int) -> Money:
        from .errors import MoneyError

        if isinstance(qty, bool) or not isinstance(qty, int):
            raise MoneyError("Money may only be multiplied by an int quantity")
        return Money(self.minor * qty, self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    # ---- comparison ---------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.minor >= other.minor

    # ---- predicates ---------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0

    # ---- splitting ----------------------------------------------------

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split exactly across weights using largest-remainder. Sum always equals self.

        Used for proportional partial refunds: no paisa is created or destroyed.
        """
        from .errors import MoneyError

        if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
            raise MoneyError("allocate requires positive weights summing above zero")
        total_w = sum(weights)
        base = [self.minor * w // total_w for w in weights]
        remainder = self.minor - sum(base)
        # distribute the remainder to the largest fractional parts, deterministically
        fracs = sorted(
            range(len(weights)),
            key=lambda i: (-((self.minor * weights[i]) % total_w), i),
        )
        for i in range(remainder):
            base[fracs[i % len(base)]] += 1
        return [Money(m, self.currency) for m in base]

    # ---- presentation (never used for arithmetic) ---------------------

    def format_decimal(self) -> str:
        """Exact decimal string, e.g. ``39500 INR -> '395.00'``. Presentation only."""
        exp = exponent_for(self.currency)
        if exp == 0:
            return str(self.minor)
        sign = "-" if self.minor < 0 else ""
        whole, frac = divmod(abs(self.minor), 10**exp)
        return f"{sign}{whole}.{frac:0{exp}d}"

    def __str__(self) -> str:
        return f"{self.format_decimal()} {self.currency}"

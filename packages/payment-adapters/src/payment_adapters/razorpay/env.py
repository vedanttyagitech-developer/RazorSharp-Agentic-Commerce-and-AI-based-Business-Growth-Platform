"""Loading :class:`RazorpayConfig` from process environment variables.

Specification 11.5 and ADR 0003 D3. The API process and the worker both build their
Razorpay configuration from the same five variables, and both must fail *at startup* if
the variables are wrong -- a process that starts with a live key and finds out on the
first checkout has already had the chance to move real money.

This module reads a ``Mapping``, not ``os.environ``, on purpose. A caller passes
``os.environ`` in production and a plain ``dict`` in a test, so the guard can be proven
without mutating the interpreter's environment, and no test can accidentally pick up a
developer's real ``.env``.

All validation lives in :class:`RazorpayConfig` itself. This module only names variables
and reports which one is missing; it adds no second guard that could drift from the first.
The one rule it owns is that an error message names the *variable* and never its value:
the value of ``RAZORPAY_KEY_SECRET`` is precisely what must not appear in the log line
that says it was malformed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .config import RazorpayConfig, RazorpayProfile
from .errors import ConfigurationError

__all__ = [
    "API_CREDENTIAL_VARIABLE",
    "KEY_ID_VARIABLE",
    "PRODUCTION_APPROVAL_VARIABLE",
    "PROFILE_VARIABLE",
    "REQUIRED_VARIABLES",
    "WEBHOOK_CREDENTIAL_VARIABLE",
    "load_config_from_env",
]

KEY_ID_VARIABLE: Final[str] = "RAZORPAY_KEY_ID"
API_CREDENTIAL_VARIABLE: Final[str] = "RAZORPAY_KEY_SECRET"
WEBHOOK_CREDENTIAL_VARIABLE: Final[str] = "RAZORPAY_WEBHOOK_SECRET"
PROFILE_VARIABLE: Final[str] = "RAZORPAY_PROFILE"
#: Read only when the profile is ``PRODUCTION``; ``RazorpayConfig`` insists on it there.
PRODUCTION_APPROVAL_VARIABLE: Final[str] = "RAZORPAY_PRODUCTION_APPROVAL_REF"

#: Variables without which no configuration can be built. ``RAZORPAY_PROFILE`` is not
#: among them: its default is ``DEVELOPMENT``, the most restrictive profile, so a process
#: that forgets to set it gets test-keys-only rather than the most permissive behaviour.
REQUIRED_VARIABLES: Final[tuple[str, ...]] = (
    KEY_ID_VARIABLE,
    API_CREDENTIAL_VARIABLE,
    WEBHOOK_CREDENTIAL_VARIABLE,
)


def _require(environ: Mapping[str, str], name: str) -> str:
    """Return the variable's value, stripped, or raise naming the variable.

    Whitespace is stripped because a trailing newline is how a secret arrives from a
    ``.env`` file edited by hand or a secret manager that appends one, and an HMAC keyed
    on ``secret\\n`` verifies nothing. A value that is whitespace-only counts as missing.
    """
    value = environ.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(
            f"{name} is not set; the Razorpay adapter cannot start without it (specification 11.5)"
        )
    return value.strip()


def _profile(environ: Mapping[str, str]) -> RazorpayProfile:
    raw = environ.get(PROFILE_VARIABLE)
    if raw is None or not raw.strip():
        return RazorpayProfile.DEVELOPMENT
    try:
        return RazorpayProfile(raw.strip().upper())
    except ValueError:
        allowed = ", ".join(p.value for p in RazorpayProfile)
        # The value is deliberately not echoed. It is not a secret, but the habit of
        # rendering environment values into exceptions is how the next variable's value
        # ends up in a log, and naming the allowed set is enough to fix it.
        raise ConfigurationError(f"{PROFILE_VARIABLE} must be one of {allowed}") from None


def load_config_from_env(environ: Mapping[str, str]) -> RazorpayConfig:
    """Build a validated :class:`RazorpayConfig` from environment variables.

    Reads ``RAZORPAY_KEY_ID``, ``RAZORPAY_KEY_SECRET``, ``RAZORPAY_WEBHOOK_SECRET`` and
    ``RAZORPAY_PROFILE`` (default ``DEVELOPMENT``, case-insensitive), plus
    ``RAZORPAY_PRODUCTION_APPROVAL_REF`` which only matters under ``PRODUCTION``.

    Guarantees:

    * a missing or blank required variable raises ``ConfigurationError`` naming the
      variable and nothing else -- never a value, never a fragment of one;
    * a ``rzp_live_`` key under ``DEVELOPMENT`` or ``DEMO`` raises ``ConfigurationError``,
      through the same ``RazorpayConfig`` guard that direct construction uses, so there
      is exactly one place that decides what a live key needs;
    * the returned config's ``repr`` and ``str`` are redacted, so logging it at startup
      is safe.

    Refuses to fall back to a default for any credential. There is no "test key for
    local use" baked in here: a developer without credentials gets a clear startup error,
    not a process that appears to work against nothing.
    """
    key_id = _require(environ, KEY_ID_VARIABLE)
    key_secret = _require(environ, API_CREDENTIAL_VARIABLE)
    webhook_secret = _require(environ, WEBHOOK_CREDENTIAL_VARIABLE)
    profile = _profile(environ)
    approval = environ.get(PRODUCTION_APPROVAL_VARIABLE)
    approval_ref = approval.strip() if approval and approval.strip() else None

    return RazorpayConfig.load(
        key_id=key_id,
        key_secret=key_secret,
        webhook_secret=webhook_secret,
        profile=profile,
        production_approval_ref=approval_ref,
    )

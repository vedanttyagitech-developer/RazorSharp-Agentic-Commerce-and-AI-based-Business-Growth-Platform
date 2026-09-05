"""Razorpay credentials and the guard that keeps a live key out of a demo.

Specification 11.5. The failure this module exists to prevent is mundane and expensive:
somebody pastes a ``rzp_live_`` key into a ``.env`` on a laptop, runs the demo, and a
real card is charged for a scripted basket. There is no undo for that, so the check runs
at configuration load and refuses to produce a usable config at all.

The guard is deliberately asymmetric in one direction and symmetric in the other:

* a **live key** is refused unless the profile is ``PRODUCTION`` *and* a named approval
  reference is supplied. Two independent facts must line up, because a single boolean is
  exactly the kind of thing that gets flipped to unblock a broken pipeline;
* a **test key** is refused *in* ``PRODUCTION``, because a production deployment silently
  running against test mode takes no money at all and reports success while doing it.

Secrets never reach a log. ``__repr__`` is overridden rather than trusted to reviewers,
because the value that gets printed is almost never printed on purpose -- it arrives in
a traceback, a structured log field, or an exception message someone re-raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .errors import ConfigurationError

__all__ = [
    "API_BASE_URL",
    "LIVE_KEY_PREFIX",
    "TEST_KEY_PREFIX",
    "RazorpayConfig",
    "RazorpayProfile",
]

#: Razorpay key identifiers are prefixed by mode. This is the only signal available
#: before a request is made, which is why it is checked before one ever is.
TEST_KEY_PREFIX: Final[str] = "rzp_test_"
LIVE_KEY_PREFIX: Final[str] = "rzp_live_"

API_BASE_URL: Final[str] = "https://api.razorpay.com/v1"

#: Shortest credential we will accept. Not a strength claim -- a real Razorpay secret is
#: far longer -- but it stops an empty or placeholder value from being loaded, and an
#: empty HMAC key would still produce signatures that "verify" against anything an
#: attacker computes with the same empty key.
_MIN_SECRET_LENGTH: Final[int] = 8


class RazorpayProfile(StrEnum):
    """Which environment this process believes it is running in.

    ``DEVELOPMENT`` and ``DEMO`` are distinct so that a deployed demo can be recognised
    in logs and metrics, but they carry identical authority: test keys only.
    """

    DEVELOPMENT = "DEVELOPMENT"
    DEMO = "DEMO"
    PRODUCTION = "PRODUCTION"


#: Profiles in which only ``rzp_test_`` credentials may be loaded.
_TEST_ONLY_PROFILES: Final[frozenset[RazorpayProfile]] = frozenset(
    {RazorpayProfile.DEVELOPMENT, RazorpayProfile.DEMO}
)


def _redact(value: str) -> str:
    """Render a secret as its length only. Never its prefix, never its suffix.

    Showing the first four characters is a common habit and a bad one: for a credential
    with a known fixed prefix it discloses nothing useful for debugging while training
    everyone to expect fragments of secrets in logs.
    """
    return f"<redacted len={len(value)}>"


@dataclass(frozen=True, slots=True, repr=False)
class RazorpayConfig:
    """Verified Razorpay credentials for one tenant-independent process.

    Guarantees, once an instance exists:

    * ``key_id`` matches the mode the profile permits;
    * ``key_secret`` and ``webhook_secret`` are non-empty and are *different values*;
    * ``is_test_mode`` is true for every non-production profile.

    Refuses to construct at all when any of those fail. There is no "warn and continue"
    branch, because the operator who would have read the warning is not present when a
    background worker starts.

    Construct through :meth:`load`. Direct construction is validated identically, so a
    ``dataclasses.replace`` cannot smuggle a live key past the guard.
    """

    key_id: str
    key_secret: str
    #: ``None`` means *this process does not verify webhooks* -- the durable worker's role.
    #: It talks to Razorpay outbound and never receives a delivery, so the deployment
    #: withholds the webhook secret from it on purpose (least privilege, ADR 0003 D3). This
    #: field used to be required, which made the worker refuse to start with exactly the
    #: secrets its own manifest grants it: a crash loop in the one process that moves money.
    #: The API, which does verify webhooks, requires the secret at its own settings layer
    #: (``commerce_api.settings``, no default), so relaxing it here weakens nothing for the
    #: process that needs it.
    webhook_secret: str | None
    profile: RazorpayProfile
    #: Identifier of the recorded human approval that permits live credentials, per
    #: specification 11.5 ("an explicit production profile *and* separate approval").
    #: Never consulted outside ``PRODUCTION``.
    production_approval_ref: str | None = None
    base_url: str = API_BASE_URL

    def __post_init__(self) -> None:
        self._check_key_id()
        self._check_secrets()

    # ---- validation ----------------------------------------------------

    def _check_key_id(self) -> None:
        if not self.key_id:
            raise ConfigurationError("RAZORPAY_KEY_ID is empty")

        is_test = self.key_id.startswith(TEST_KEY_PREFIX)
        is_live = self.key_id.startswith(LIVE_KEY_PREFIX)

        if not is_test and not is_live:
            # An unrecognised prefix is refused rather than assumed to be test mode.
            # Assuming test mode is how an unknown future live prefix gets waved through.
            raise ConfigurationError(
                f"RAZORPAY_KEY_ID must start with {TEST_KEY_PREFIX!r} or {LIVE_KEY_PREFIX!r}; "
                "an unrecognised key prefix is never assumed to be test mode"
            )

        if self.profile in _TEST_ONLY_PROFILES:
            if not is_test:
                raise ConfigurationError(
                    f"profile {self.profile.value} accepts only {TEST_KEY_PREFIX!r} keys; "
                    "a live key in a development or demo process charges real money"
                )
            return

        # PRODUCTION from here.
        if is_test:
            raise ConfigurationError(
                f"profile {RazorpayProfile.PRODUCTION.value} rejects {TEST_KEY_PREFIX!r} keys; "
                "a production deployment in test mode reports success while taking no money"
            )
        if not self.production_approval_ref:
            raise ConfigurationError(
                "live credentials require production_approval_ref naming the recorded "
                "approval; the production profile alone is not sufficient authority "
                "(specification 11.5)"
            )

    def _check_secrets(self) -> None:
        if len(self.key_secret) < _MIN_SECRET_LENGTH:
            raise ConfigurationError(
                f"RAZORPAY_KEY_SECRET is missing or shorter than {_MIN_SECRET_LENGTH} characters"
            )
        if self.webhook_secret is None:
            # No webhook secret at all is the worker's honest state, not a misconfiguration.
            # There is nothing to length-check and nothing to compare against, so the two
            # checks below do not apply. An *empty string* is still refused by them: absent
            # and blank are different facts, and only absent is allowed.
            return
        if len(self.webhook_secret) < _MIN_SECRET_LENGTH:
            raise ConfigurationError(
                f"RAZORPAY_WEBHOOK_SECRET is missing or shorter than "
                f"{_MIN_SECRET_LENGTH} characters"
            )
        if self.key_secret == self.webhook_secret:
            # Specification 11.5 requires these to be separate material. Sharing them
            # means anyone who can verify a webhook can also sign API calls, so a leak of
            # the webhook secret from an edge log becomes full payment authority.
            raise ConfigurationError(
                "RAZORPAY_WEBHOOK_SECRET must differ from RAZORPAY_KEY_SECRET; "
                "shared material turns a webhook-secret leak into API authority"
            )

    # ---- accessors -----------------------------------------------------

    @property
    def is_test_mode(self) -> bool:
        """True when these credentials can only ever move test-mode money."""
        return self.key_id.startswith(TEST_KEY_PREFIX)

    def orders_url(self) -> str:
        return f"{self.base_url}/orders"

    def refunds_url(self, payment_id: str) -> str:
        return f"{self.base_url}/payments/{payment_id}/refund"

    def order_lookup_url(self, receipt: str) -> str:
        """Lookup by stable receipt, the recovery path after a lost create-order response.

        Specification 10.6 requires this query *before* any second create, which is the
        only thing standing between a timed-out create and two provider orders for one
        checkout version.
        """
        from urllib.parse import quote

        return f"{self.base_url}/orders?receipt={quote(receipt, safe='')}"

    # ---- construction --------------------------------------------------

    @classmethod
    def load(
        cls,
        *,
        key_id: str,
        key_secret: str,
        webhook_secret: str | None = None,
        profile: RazorpayProfile = RazorpayProfile.DEVELOPMENT,
        production_approval_ref: str | None = None,
        base_url: str = API_BASE_URL,
    ) -> RazorpayConfig:
        """Validate credentials and return a config, or raise ``ConfigurationError``.

        The default profile is ``DEVELOPMENT``, so a caller that forgets to pass one gets
        the most restrictive behaviour rather than the most permissive.
        """
        return cls(
            key_id=key_id,
            key_secret=key_secret,
            webhook_secret=webhook_secret,
            profile=profile,
            production_approval_ref=production_approval_ref,
            base_url=base_url,
        )

    def require_webhook_secret(self) -> str:
        """The webhook secret, for the one kind of process that verifies deliveries.

        The field is optional on the config because the durable worker is legitimately
        built without it. A *receiver* is not: the API verifies HMAC on every delivery, and
        a receiver with nothing to verify against must not report a forgery-shaped
        ``AUTHORITY_INSUFFICIENT`` -- from outside that is indistinguishable from an attack.
        So this raises the real cause instead, and every receiver reads the secret through
        it. The worker never calls this.
        """
        if self.webhook_secret is None:
            raise ConfigurationError(
                "a webhook is being verified but RAZORPAY_WEBHOOK_SECRET is not configured "
                "for this process; only the API receives webhooks and it must hold the secret"
            )
        return self.webhook_secret

    # ---- presentation --------------------------------------------------

    def __repr__(self) -> str:
        """Redacted representation. The only representation this object has."""
        return (
            f"RazorpayConfig(key_id={_redact(self.key_id)}, "
            f"key_secret={_redact(self.key_secret)}, "
            f"webhook_secret="
            f"{'<absent>' if self.webhook_secret is None else _redact(self.webhook_secret)}, "
            f"profile={self.profile.value}, test_mode={self.is_test_mode})"
        )

    __str__ = __repr__

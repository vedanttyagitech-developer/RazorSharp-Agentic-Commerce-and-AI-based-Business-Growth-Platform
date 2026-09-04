"""Process configuration, validated once at startup and never guessed at afterwards.

Three of the guards here exist because the alternative is a money bug rather than an
outage:

* **Two database URLs, both required, neither defaulted.** ADR 0003 D1 makes API
  mutations run as ``commerce_kernel`` and reads run as ``commerce_app``, and the
  physical boundary between them is the database grant set. A single ``DATABASE_URL``
  that both paths fall back to erases that boundary silently: everything still works, the
  app role's inability to write ``payment_attempts`` stops being tested by production,
  and nobody finds out until an audit asks which role wrote a row. So both are named,
  both are required, and there is no fallback to read.

* **``WEB_CONCURRENCY`` may not exceed 1** (ADR 0003 D14). The merchant simulator holds
  authoritative catalogue and inventory state in this process's memory. A second worker
  process would hold a *different* copy, so a price injected in step 5 of the
  demonstration would be visible to some requests and not others, and the reapproval it
  is supposed to trigger would fire at random. This is a documented demo restriction, not
  an architectural claim: GKE runs one replica of the API and one of the worker.

* **The Razorpay credentials are built by ``payment_adapters.load_config_from_env``**,
  which refuses a live key outside an explicit production profile with a named approval
  (specification 11.5). That guard is not re-implemented here. This module only decides
  which mapping to hand it, so there is exactly one place that knows what a live key
  requires.

Everything is constructible from a plain dictionary. A test builds a ``Settings`` without
touching ``os.environ`` and therefore cannot pick up a developer's real ``.env``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any, Final, Self

from payment_adapters import RazorpayConfig, RazorpayProfile, load_config_from_env
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "Profile",
    "Settings",
    "get_settings",
]

#: One hour. Long enough for a demonstration run, short enough that a token found in a
#: log after the fact is already useless.
DEFAULT_SESSION_TTL_SECONDS: Final[int] = 3600


class Profile(StrEnum):
    """Which environment this process believes it is.

    Maps one-to-one onto :class:`payment_adapters.RazorpayProfile`, so a single
    ``PROFILE`` setting decides both what routes exist and which Razorpay keys are
    acceptable. Two independent switches would eventually disagree, and the disagreement
    everyone discovers late is "demo routes enabled, live keys loaded".
    """

    DEVELOPMENT = "development"
    DEMO = "demo"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self is Profile.PRODUCTION

    def as_razorpay(self) -> RazorpayProfile:
        return RazorpayProfile(self.value.upper())


class Settings(BaseSettings):
    """Everything this process needs to start, and nothing it can discover later.

    Construct from the environment with :func:`get_settings`, or from a dictionary in a
    test::

        Settings(
            PROFILE="development",
            DATABASE_URL_APP="postgresql+psycopg://...",
            DATABASE_URL_KERNEL="postgresql+psycopg://...",
            RAZORPAY_KEY_ID="rzp_test_...",
            RAZORPAY_KEY_SECRET="...",
            RAZORPAY_WEBHOOK_SECRET="...",
        )

    Field names are lower case; the environment variables are the upper-case aliases
    shown above, which is why every field carries an explicit alias rather than relying
    on a prefix.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
        # No env_file: a process is configured by its environment or by an explicit
        # mapping. Reading a file from the working directory makes the same code behave
        # differently depending on where it was launched from.
    )

    profile: Profile = Field(default=Profile.DEVELOPMENT, validation_alias="PROFILE")

    #: Reads. Connects as ``commerce_app``: SELECT everywhere, writes only on the
    #: non-financial head tables.
    database_url_app: str = Field(validation_alias="DATABASE_URL_APP")
    #: Mutations. Connects as ``commerce_kernel``, the only role that may write a
    #: financial table.
    database_url_kernel: str = Field(validation_alias="DATABASE_URL_KERNEL")

    #: Guards the scenario controller and the operator views (ADR 0003 D11). ``None``
    #: means those routes do not exist in this process at all.
    scenario_key: SecretStr | None = Field(default=None, validation_alias="SCENARIO_KEY")

    session_ttl_seconds: int = Field(
        default=DEFAULT_SESSION_TTL_SECONDS, validation_alias="SESSION_TTL_SECONDS", gt=0
    )

    #: See the module docstring. Refused above 1.
    web_concurrency: int = Field(default=1, validation_alias="WEB_CONCURRENCY", ge=1)

    razorpay_key_id: str = Field(validation_alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: SecretStr = Field(validation_alias="RAZORPAY_KEY_SECRET")
    razorpay_webhook_secret: SecretStr = Field(validation_alias="RAZORPAY_WEBHOOK_SECRET")
    razorpay_production_approval_ref: str | None = Field(
        default=None, validation_alias="RAZORPAY_PRODUCTION_APPROVAL_REF"
    )

    # ---- validation ----------------------------------------------------

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Refuse a configuration that would be wrong rather than merely unusual.

        Runs after field validation so the messages can name more than one field at
        once, and so the Razorpay guard runs against a profile that is already parsed.
        """
        if self.web_concurrency > 1:
            raise ValueError(
                f"WEB_CONCURRENCY is {self.web_concurrency}; this API must run as a single "
                "process (ADR 0003 D14). The merchant simulator's catalogue and inventory "
                "live in process memory, so a second worker would serve a different "
                "merchant state and the step-5 price change would apply to some requests "
                "only. Scale with one replica per process instead."
            )
        for name, url in (
            ("DATABASE_URL_APP", self.database_url_app),
            ("DATABASE_URL_KERNEL", self.database_url_kernel),
        ):
            if not url.strip():
                raise ValueError(f"{name} is empty; a role's connection URL is never defaulted")
        # Builds and validates the credentials now, so a live key in a demo profile stops
        # the process at startup rather than at the first checkout.
        self.razorpay()
        return self

    # ---- derived -------------------------------------------------------

    def razorpay(self) -> RazorpayConfig:
        """The validated Razorpay credentials for this process.

        Delegates every rule to :func:`payment_adapters.load_config_from_env`: a live key
        needs ``PROFILE=production`` *and* ``RAZORPAY_PRODUCTION_APPROVAL_REF``, a test
        key is refused in production, and the API secret must differ from the webhook
        secret. Rebuilt on each call rather than cached, because the returned object is
        cheap, frozen, and redacted in ``repr`` -- caching it would only add a place where
        a rotated secret goes stale.
        """
        environ: dict[str, str] = {
            "RAZORPAY_KEY_ID": self.razorpay_key_id,
            "RAZORPAY_KEY_SECRET": self.razorpay_key_secret.get_secret_value(),
            "RAZORPAY_WEBHOOK_SECRET": self.razorpay_webhook_secret.get_secret_value(),
            "RAZORPAY_PROFILE": self.profile.as_razorpay().value,
        }
        if self.razorpay_production_approval_ref:
            environ["RAZORPAY_PRODUCTION_APPROVAL_REF"] = self.razorpay_production_approval_ref
        return load_config_from_env(environ)

    @property
    def scenario_routes_enabled(self) -> bool:
        """Whether the scenario controller exists in this process at all (D11).

        False in production, and false anywhere no key is configured: a demo apparatus
        with no key is not "open", it is absent.
        """
        return not self.profile.is_production and self.scenario_key is not None

    @property
    def demo_routes_enabled(self) -> bool:
        """Whether ``POST /v1/demo/sessions`` exists. Never in production."""
        return not self.profile.is_production

    def __repr__(self) -> str:
        """Redacted. The only representation these settings have.

        Secrets are :class:`SecretStr`, so they redact themselves, but a connection URL
        carries a password in its authority section and pydantic has no opinion about
        that. Naming the fields without their values is enough to debug a start-up.
        """
        return (
            f"Settings(profile={self.profile.value}, "
            f"web_concurrency={self.web_concurrency}, "
            f"session_ttl_seconds={self.session_ttl_seconds}, "
            f"scenario_key={'set' if self.scenario_key else 'unset'}, "
            f"database_url_app=<redacted>, database_url_kernel=<redacted>, "
            f"razorpay_key_id={self.razorpay_key_id[:9]}...)"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings(**overrides: Any) -> Settings:
    """The process's settings, read from the environment once.

    Cached because reading is cheap but *validating* is not free and, more importantly,
    because two ``Settings`` objects in one process could disagree after an environment
    change mid-run. ``overrides`` exists for a caller that wants a distinct cached
    configuration (the worker's entry point); tests construct ``Settings`` directly
    instead, which never touches this cache.
    """
    return Settings(**overrides)

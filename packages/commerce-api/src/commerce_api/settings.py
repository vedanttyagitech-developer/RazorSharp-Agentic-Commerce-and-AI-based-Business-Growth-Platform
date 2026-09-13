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

* **``WEB_CONCURRENCY`` may not exceed 1**. Catalogue and inventory are persisted
  in PostgreSQL and mutations use database locks. The remaining restriction protects
  process-local MCP sessions, MCP/ACP rate-limit buckets and metrics collection.
  Multiple workers or replicas need shared protocol state and a metrics aggregation
  strategy before they are supported; database persistence alone is insufficient.

* **The Razorpay credentials are built by ``payment_adapters.load_config_from_env``**,
  which refuses a live key outside an explicit production profile with a named approval
  (specification 11.5). That guard is not re-implemented here. This module only decides
  which mapping to hand it, so there is exactly one place that knows what a live key
  requires.

Everything is constructible from a plain dictionary. A test builds a ``Settings`` without
touching ``os.environ`` and therefore cannot pick up a developer's real ``.env``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final, Self

from commerce_protocols.acp import AcpClient, ClientRegistry
from commerce_protocols.ap2.signing import InProcessSigner
from commerce_protocols.core import PROTOCOL_CAPABILITIES
from jwcrypto.jwk import JWK
from payment_adapters import RazorpayConfig, RazorpayProfile, load_config_from_env
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import InitSettingsSource

__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "Profile",
    "Settings",
    "UcpSigners",
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


@dataclass(frozen=True, slots=True)
class UcpSigners:
    """This deployment's two protocol signing keys, held apart on purpose.

    A pair rather than a mapping, because there are exactly two roles and the type should
    say so: a mapping invites a third entry and a lookup that can miss, and "which key
    signs a receipt" is not a question that should have a runtime answer.

    Holds private key material, so it inherits :class:`InProcessSigner`'s discipline: the
    only way out is a signature or a public JWK. It is deliberately *not* stored on
    ``app.state`` -- it is rebuilt from :meth:`Settings.ucp_signers` where it is needed,
    so there is no long-lived attribute on a shared object for something else to read.
    """

    merchant: InProcessSigner
    platform: InProcessSigner


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],  # noqa: ARG003 - pydantic-settings' fixed override signature
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Init kwargs are the WHOLE configuration, or none of it is.

        The class docstring above promises two modes -- construct from the process
        environment, or from an explicit dictionary in a test -- and states the second
        one as self-contained. Left at pydantic-settings' default source order
        (``init_settings, env_settings, ...``), that promise is false the moment a test
        omits a field the calling shell happens to also export: pydantic-settings does
        not distinguish "this key was not part of the caller's mapping" from "read it
        from wherever", so a field a test deliberately left unset -- to prove a
        ``None``-triggered refusal, or a stale value's absence -- reads the developer's
        own exported ``UCP_PLATFORM_SIGNING_JWK`` or ``SCENARIO_KEY`` instead of the
        default the test asked for. Two tests were false-green on exactly this before it
        was traced here: each proved a startup refusal by deleting one key from an
        explicit dict, and each was answered by the caller's shell instead.

        The fix is not in either test -- deleting a key from a dict is the correct way
        to ask "what if this were unset", and a class whose own docstring calls that
        mode a real construction path should honour it. So when ANY init kwarg is
        supplied, ``env_settings`` and ``dotenv_settings`` are dropped from the source
        list entirely: every field not named in that call falls straight to its
        declared ``default``, never to ``os.environ``. A field genuinely required with
        no default (``DATABASE_URL_APP`` and its neighbours) still raises exactly the
        "field required" pydantic error it always would -- construction from a partial
        dict was never a promise to fill gaps from the environment, only to leave them
        as the field's own stated default or absence.

        A construction with NO init kwargs at all -- :func:`get_settings`, the real
        process boot path -- keeps every source: that is reading from the environment,
        the other half of the docstring's promise, and this override changes nothing
        about it.
        """
        # `init_settings` is typed as the base `PydanticBaseSettingsSource` protocol, which
        # carries no `init_kwargs` attribute; only the concrete `InitSettingsSource` pydantic
        # actually passes here does. Narrowed rather than asserted with a type-ignore, so a
        # future pydantic-settings release that stops passing this concrete type fails a type
        # check here instead of an AttributeError at the next test run.
        if isinstance(init_settings, InitSettingsSource) and init_settings.init_kwargs:
            return (init_settings, file_secret_settings)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

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

    #: RFC 8707 resource indicator: the string this deployment answers to as an MCP
    #: resource server. ``None`` means the MCP transport does not exist in this process.
    #: There is no default, and that is the check rather than a formality: a resource
    #: server that guesses its own identity cannot refuse a token minted for a different
    #: one, so an audience comparison against a value nobody chose proves nothing.
    mcp_resource: str | None = Field(default=None, validation_alias="MCP_RESOURCE")

    #: Signs the short-lived access tokens the MCP token endpoint issues. Required
    #: whenever ``MCP_RESOURCE`` is set -- see :meth:`_check`.
    mcp_token_secret: SecretStr | None = Field(default=None, validation_alias="MCP_TOKEN_SECRET")

    #: The audience an ACP signature must have been minted for, and the one this
    #: deployment builds its own signing string with. ``None`` means the ACP transport
    #: does not exist here.
    acp_audience: str | None = Field(default=None, validation_alias="ACP_AUDIENCE")

    #: The external AI buyers this deployment has issued ACP credentials to, as a JSON
    #: array. Parsed and validated in :meth:`_check`, so a malformed registry stops the
    #: process at startup rather than surfacing as a refusal on the first request.
    acp_clients: str | None = Field(default=None, validation_alias="ACP_CLIENTS")

    #: The merchant's ES256 signing key, one private JWK as JSON. Signs the artifacts
    #: that say "the merchant authorised this checkout".
    #:
    #: There is deliberately no generated fallback, for the same reason
    #: ``MCP_TOKEN_SECRET`` has none, and the reason is sharper here.
    #:
    #: The fallback this replaced minted a P-256 key at first use and gave it the fixed
    #: ``kid`` ``merchant-ephemeral-1``. That combination is the worst available: the key
    #: material changed at every restart while the *name* of it did not, so evidence
    #: signed before a restart was refused afterwards at ``signature_did_not_verify`` --
    #: byte for byte the refusal a forged signature produces, not the ``unknown_kid`` an
    #: honestly rotated key would produce. An operator holding real evidence and a
    #: restarted pod could not tell the two apart. Specification 14.1 requires keys to
    #: rotate "without silently invalidating stored evidence", and *silently* is precisely
    #: what that was.
    #:
    #: ``None`` means this deployment publishes no UCP profile at all, which is honest; a
    #: generated key is not.
    ucp_merchant_signing_jwk: SecretStr | None = Field(
        default=None, validation_alias="UCP_MERCHANT_SIGNING_JWK"
    )

    #: The platform's ES256 signing key, one private JWK as JSON. Signs the artifacts that
    #: say "the platform issued this receipt".
    #:
    #: Separate from the merchant's, and required to carry a different ``kid``. Those are
    #: two different claims, and they stop being different the moment one key can produce
    #: both signatures -- at which point the AP2 verification sequence's step 3, resolve
    #: the correct key by ``kid``, is resolving nothing.
    ucp_platform_signing_jwk: SecretStr | None = Field(
        default=None, validation_alias="UCP_PLATFORM_SIGNING_JWK"
    )

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
                f"WEB_CONCURRENCY is {self.web_concurrency}; only one API process is supported. "
                "Catalogue and inventory are database-backed, but MCP sessions and MCP/ACP "
                "rate-limit buckets remain process-local. Shared protocol state and "
                "multi-process metrics validation are required before enabling multiple "
                "workers or replicas."
            )
        for name, url in (
            ("DATABASE_URL_APP", self.database_url_app),
            ("DATABASE_URL_KERNEL", self.database_url_kernel),
        ):
            if not url.strip():
                raise ValueError(f"{name} is empty; a role's connection URL is never defaulted")
        if self.mcp_resource is not None and self.mcp_token_secret is None:
            raise ValueError(
                "MCP_RESOURCE is set but MCP_TOKEN_SECRET is not. The MCP token endpoint "
                "issues signed bearer credentials, so the surface cannot be configured "
                "without the key that signs them. There is deliberately no generated "
                "fallback: a process that mints its own token secret answers to tokens "
                "nobody issued it, and the misconfiguration is invisible while it works."
            )
        if (self.acp_audience is None) != (self.acp_clients is None):
            raise ValueError(
                "ACP_AUDIENCE and ACP_CLIENTS are configured together or not at all. An "
                "audience with no registered client answers to nobody; a registry with no "
                "audience has nothing to bind its credentials to."
            )
        if (self.ucp_merchant_signing_jwk is None) != (self.ucp_platform_signing_jwk is None):
            raise ValueError(
                "UCP_MERCHANT_SIGNING_JWK and UCP_PLATFORM_SIGNING_JWK are configured "
                "together or not at all. The profiles are published as a pair, and a "
                "deployment that signed merchant artifacts but could not sign platform "
                "ones would advertise half a protocol surface -- with the missing half "
                "looking, to a counterparty, exactly like a key it failed to fetch."
            )
        # Builds and validates the credentials now, so a live key in a demo profile stops
        # the process at startup rather than at the first checkout. The ACP registry is
        # built for the same reason: a client whose tenant id will not parse should stop
        # the process, not become a 500 on somebody's first signed request.
        self.razorpay()
        self.acp_registry()
        # And the signers, for a third reason on top of that one: a key that will not load
        # must not be discovered by a counterparty fetching the JWK Set. Failing to start
        # is a deployment that never serves; failing at first fetch is a deployment that
        # serves everything except the ability to verify what it signed.
        self.ucp_signers()
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

    def acp_registry(self) -> ClientRegistry:
        """The external AI buyers this deployment answers to. Empty when unconfigured.

        Every field is required except the three that are genuinely optional on
        :class:`~commerce_protocols.acp.AcpClient`, and an unknown field is refused rather
        than ignored: a registry entry that says ``"tennant_id"`` would otherwise
        silently take the default for the field it meant to set, and a client bound to the
        wrong tenant is the one configuration mistake this file exists to make impossible.

        ``granted`` is intersected with the protocol ceiling here as well as inside
        ``principal_for``. Not because either is unreliable, but because a registry that
        *names* ``checkout.approve`` should be legible as a mistake at the point it is
        read, and an operator comparing the configured list against the effective one
        should be comparing two sets that were narrowed the same way.

        Rebuilt on each call rather than cached, matching :meth:`razorpay`: the object is
        small and frozen, and a cache is one more place a rotated secret goes stale.
        """
        if self.acp_clients is None or self.acp_audience is None:
            return ClientRegistry.of()
        try:
            entries = json.loads(self.acp_clients)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ACP_CLIENTS is not valid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise ValueError("ACP_CLIENTS is a JSON array of client objects")
        return ClientRegistry.of(
            *(self._acp_client(entry, index) for index, entry in enumerate(entries))
        )

    def _acp_client(self, entry: Any, index: int) -> AcpClient:
        """One registry entry, or a message naming which entry is wrong."""
        where = f"ACP_CLIENTS[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not an object")
        known = {
            "client_id",
            "tenant_id",
            "merchant_id",
            "signing_secret",
            "api_key_digest",
            "buyer_ref",
            "granted",
        }
        unknown = sorted(set(entry) - known)
        if unknown:
            raise ValueError(
                f"{where} names fields this registry does not understand: {unknown}. "
                f"Known fields are {sorted(known)}."
            )
        missing = sorted({"client_id", "tenant_id", "merchant_id", "signing_secret"} - set(entry))
        if missing:
            raise ValueError(f"{where} is missing required fields: {missing}")
        granted = entry.get("granted")
        if granted is not None and not isinstance(granted, list):
            raise ValueError(f"{where}.granted is a list of capability names")
        try:
            return AcpClient(
                client_id=str(entry["client_id"]),
                tenant_id=uuid.UUID(str(entry["tenant_id"])),
                merchant_id=uuid.UUID(str(entry["merchant_id"])),
                # The audience is this deployment's, never the entry's. A client that
                # could name its own audience would be agreeing with itself, and audience
                # binding would stop being a check.
                audience=str(self.acp_audience),
                signing_secret=str(entry["signing_secret"]).encode("utf-8"),
                api_key_digest=(
                    None if entry.get("api_key_digest") is None else str(entry["api_key_digest"])
                ),
                buyer_ref=None if entry.get("buyer_ref") is None else str(entry["buyer_ref"]),
                granted=(
                    PROTOCOL_CAPABILITIES
                    if granted is None
                    else frozenset(str(c) for c in granted) & PROTOCOL_CAPABILITIES
                ),
            )
        except ValueError as exc:
            raise ValueError(f"{where} is not a usable client: {exc}") from exc

    def ucp_signers(self) -> UcpSigners | None:
        """The merchant and platform signing keys, or ``None`` when unconfigured.

        Rebuilt on each call, matching :meth:`razorpay` and :meth:`acp_registry`. The old
        router cached its keys because it generated them, and a JWK Set that changed
        between two fetches reads as tampering; a *configured* key needs no cache to be
        stable across a request, a restart or a replica, which is the whole point of the
        change. What a cache would add is one more place a rotated key goes stale.

        The two keys are required to differ. Not merely to be two configured values --
        an operator who pasted the same JWK into both variables would produce a
        deployment where "the merchant authorised this checkout" and "the platform issued
        this receipt" are the same signature, and nothing downstream could tell them
        apart afterwards. :class:`KeyRing.of` refuses a ``kid`` collision *within* a ring;
        these are two separate rings, so the collision between them has to be refused
        here or nowhere.
        """
        if self.ucp_merchant_signing_jwk is None or self.ucp_platform_signing_jwk is None:
            return None
        merchant = self._ucp_signer("UCP_MERCHANT_SIGNING_JWK", self.ucp_merchant_signing_jwk)
        platform = self._ucp_signer("UCP_PLATFORM_SIGNING_JWK", self.ucp_platform_signing_jwk)
        if merchant.kid == platform.kid:
            raise ValueError(
                f"UCP_MERCHANT_SIGNING_JWK and UCP_PLATFORM_SIGNING_JWK both carry kid "
                f"{merchant.kid!r}. A verifier resolves keys by kid, so one kid covering "
                "both roles makes 'the merchant signed this' and 'the platform signed "
                "this' the same claim. Configure two keys with two key ids."
            )
        return UcpSigners(merchant=merchant, platform=platform)

    @staticmethod
    def _ucp_signer(where: str, configured: SecretStr) -> InProcessSigner:
        """One signer from one JWK, or a message naming which variable is wrong.

        ``InProcessSigner.from_jwk`` already refuses a public key, a wrong curve and a
        missing ``kid``; this only adds which environment variable to go and look at,
        because the underlying message says "signing key" and a deployment has two.
        """
        try:
            return InProcessSigner.from_jwk(JWK.from_json(configured.get_secret_value()))
        except ValueError as exc:
            raise ValueError(f"{where} is not a usable signing key: {exc}") from exc
        except Exception as exc:
            # jwcrypto raises its own exception family for malformed JSON and unsupported
            # key types. An operator does not care which; they care that this variable is
            # the one to fix. The original is chained, never formatted in -- a JWK parse
            # error can quote the key material it was handed.
            raise ValueError(
                f"{where} is not a well-formed JWK. It holds one private P-256 signing "
                "key as JSON, carrying a kid."
            ) from exc

    @property
    def ucp_profiles_enabled(self) -> bool:
        """Whether this deployment publishes UCP profiles at all.

        False when no signing key is configured, and the well-known documents answer 404
        in that case rather than publishing an empty or a freshly minted JWK Set. Same
        reasoning as the transports below (ADR 0003 D11): a deployment that can sign
        nothing has no profile, and the honest answer to "fetch your verification keys"
        is that there are none, not a set that expires at the next restart.
        """
        return self.ucp_merchant_signing_jwk is not None and (
            self.ucp_platform_signing_jwk is not None
        )

    @property
    def mcp_routes_enabled(self) -> bool:
        """Whether the MCP transport exists in this process at all.

        False when no resource indicator is configured, and the routes answer 404 rather
        than 401 in that case for the reason ADR 0003 D11 gives about the scenario
        controller: an unconfigured surface is absent, not merely locked, and a 401 would
        announce that it is there.
        """
        return self.mcp_resource is not None and self.mcp_token_secret is not None

    @property
    def acp_routes_enabled(self) -> bool:
        """Whether the ACP transport exists in this process at all. See above."""
        return self.acp_audience is not None and self.acp_clients is not None

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
            f"mcp_resource={self.mcp_resource or 'unset'}, "
            f"acp_audience={self.acp_audience or 'unset'}, "
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

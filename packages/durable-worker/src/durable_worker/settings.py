"""What the worker process is told at startup, and the runtime assembled from it.

The worker holds **two** database identities and they are not interchangeable (ADR 0003
D1). ``DATABASE_URL_WORKER`` connects as ``commerce_worker``: it may lease outbox rows,
stamp the webhook inbox and consume scenario faults, and the database refuses it INSERT
or UPDATE on any financial table. ``DATABASE_URL_KERNEL`` connects as ``commerce_kernel``
and is used only to call kernel functions. Both are required and neither falls back to
the other, because a single URL used for both erases the one boundary that makes "only
the kernel writes money" a fact about the deployment rather than a claim about the code.

Two further guards live here rather than in a handler:

* **Razorpay credentials are built by** :func:`payment_adapters.load_config_from_env`, so
  a ``rzp_live_`` key outside an explicit production profile with a named approval stops
  this process at startup (specification 11.5). The worker is the only process that calls
  Razorpay, which makes it the one where that guard matters most.
* **Scenario faults are demo apparatus** and are only read outside the production
  profile. A fault row that could fire in production would be a way to make a real
  payment call vanish.

Everything is constructible from a plain mapping, so a test builds a
:class:`WorkerSettings` without touching ``os.environ`` and cannot pick up a developer's
real ``.env``.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final, Self

from durable_work import DEFAULT_POLICY, RetryPolicy
from payment_adapters import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpTransport,
    RazorpayConfig,
    RazorpayProfile,
    load_config_from_env,
)
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from .transport import HttpxTransport

__all__ = [
    "DEFAULT_HOUSEKEEPING_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_RECONCILIATION_BACKOFF_SECONDS",
    "Profile",
    "WorkerRuntime",
    "WorkerSettings",
    "build_runtime",
    "default_worker_id",
    "engine_for",
    "get_settings",
    "session_scope_for",
]

#: ADR D13. The outbox's own default is the same number; it is named here so the worker's
#: configuration says what it takes rather than inheriting it silently.
DEFAULT_LEASE_SECONDS: Final[int] = 60

#: How often the housekeeping sweeps run. They are hygiene, never correctness: a grant
#: past due is already unconsumable and a lapsed reservation already holds nothing.
DEFAULT_HOUSEKEEPING_SECONDS: Final[int] = 60

#: Base of the exponential backoff between reconciliation rounds (ADR D13). Six rounds at
#: this base reach roughly two and a half minutes, which is inside a demonstration's
#: patience and outside a provider's usual settling time.
DEFAULT_RECONCILIATION_BACKOFF_SECONDS: Final[int] = 5


class Profile(StrEnum):
    """Which environment this process believes it is.

    Mirrors ``commerce_api.settings.Profile`` by value on purpose: one ``PROFILE``
    variable configures the API and the worker, and two switches that could disagree
    would eventually disagree in the direction nobody wants.
    """

    DEVELOPMENT = "development"
    DEMO = "demo"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self is Profile.PRODUCTION

    def as_razorpay(self) -> RazorpayProfile:
        return RazorpayProfile(self.value.upper())


def default_worker_id() -> str:
    """Host and process, which is what an operator needs to find a stuck lease.

    A random id would be unique but would not answer "which pod is holding this?", and a
    constant one would make two replicas look like a single worker in the outbox rows.
    """
    return f"{socket.gethostname()}-{os.getpid()}"[:128]


class WorkerSettings(BaseSettings):
    """Everything the worker needs to start, and nothing it may discover later."""

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
        # No env_file, for the same reason as the API: a process is configured by its
        # environment or by an explicit mapping, never by the working directory.
    )

    profile: Profile = Field(default=Profile.DEVELOPMENT, validation_alias="PROFILE")

    #: Leases outbox rows and stamps the webhook inbox. Physically unable to write a
    #: financial table.
    database_url_worker: str = Field(validation_alias="DATABASE_URL_WORKER")
    #: Calls kernel functions. The only identity that may write money.
    database_url_kernel: str = Field(validation_alias="DATABASE_URL_KERNEL")

    worker_id: str = Field(default_factory=default_worker_id, validation_alias="WORKER_ID")
    batch_size: int = Field(default=10, validation_alias="WORKER_BATCH_SIZE", ge=1, le=500)
    lease_seconds: int = Field(
        default=DEFAULT_LEASE_SECONDS, validation_alias="WORKER_LEASE_SECONDS", ge=1, le=3600
    )
    max_attempts: int = Field(default=8, validation_alias="WORKER_MAX_ATTEMPTS", ge=1, le=64)
    poll_interval_seconds: float = Field(
        default=1.0, validation_alias="WORKER_POLL_SECONDS", gt=0.0, le=60.0
    )
    housekeeping_interval_seconds: int = Field(
        default=DEFAULT_HOUSEKEEPING_SECONDS,
        validation_alias="WORKER_HOUSEKEEPING_SECONDS",
        ge=0,
    )
    provider_timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        validation_alias="PROVIDER_TIMEOUT_SECONDS",
        gt=0.0,
        le=120.0,
    )
    reconciliation_backoff_seconds: int = Field(
        default=DEFAULT_RECONCILIATION_BACKOFF_SECONDS,
        validation_alias="RECONCILIATION_BACKOFF_SECONDS",
        ge=0,
        le=3600,
    )

    razorpay_key_id: str = Field(validation_alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: SecretStr = Field(validation_alias="RAZORPAY_KEY_SECRET")
    # Optional, and the only one of the three that is. The worker never verifies a webhook
    # -- the API does -- so the deployment withholds this secret from it on purpose. Making
    # it required here meant the worker refused to start with exactly the secrets its own
    # manifest grants it, and crash-looped in the one process that talks to Razorpay.
    razorpay_webhook_secret: SecretStr | None = Field(
        default=None, validation_alias="RAZORPAY_WEBHOOK_SECRET"
    )
    razorpay_production_approval_ref: str | None = Field(
        default=None, validation_alias="RAZORPAY_PRODUCTION_APPROVAL_REF"
    )

    # ---- validation ----------------------------------------------------

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Refuse a configuration that would be wrong rather than merely unusual."""
        for name, url in (
            ("DATABASE_URL_WORKER", self.database_url_worker),
            ("DATABASE_URL_KERNEL", self.database_url_kernel),
        ):
            if not url.strip():
                raise ValueError(f"{name} must not be blank; the worker holds two identities")
        if self.database_url_worker == self.database_url_kernel:
            raise ValueError(
                "DATABASE_URL_WORKER and DATABASE_URL_KERNEL must be different roles "
                "(ADR 0003 D1); one URL for both erases the grant boundary that makes "
                "'only the kernel writes financial tables' true of the deployment"
            )
        if not self.worker_id.strip():
            raise ValueError("WORKER_ID must not be blank; it identifies the lease holder")
        # Raises payment_adapters.ConfigurationError, deliberately not wrapped: its
        # message names the exact rule a live key breaks, and a ValueError around it
        # would bury the one sentence an operator needs.
        self.razorpay()
        return self

    # ---- accessors -----------------------------------------------------

    def razorpay(self) -> RazorpayConfig:
        """Validated Razorpay credentials, through the adapter's own guard."""
        return load_config_from_env(
            {
                "RAZORPAY_KEY_ID": self.razorpay_key_id,
                "RAZORPAY_KEY_SECRET": self.razorpay_key_secret.get_secret_value(),
                **(
                    {"RAZORPAY_WEBHOOK_SECRET": self.razorpay_webhook_secret.get_secret_value()}
                    if self.razorpay_webhook_secret is not None
                    else {}
                ),
                "RAZORPAY_PROFILE": self.profile.as_razorpay().value,
                **(
                    {"RAZORPAY_PRODUCTION_APPROVAL_REF": self.razorpay_production_approval_ref}
                    if self.razorpay_production_approval_ref
                    else {}
                ),
            }
        )

    def retry_policy(self) -> RetryPolicy:
        """The outbox policy this worker leases and fails under."""
        return RetryPolicy(
            max_attempts=self.max_attempts,
            lease_seconds=self.lease_seconds,
            backoff_base_seconds=DEFAULT_POLICY.backoff_base_seconds,
            backoff_cap_seconds=DEFAULT_POLICY.backoff_cap_seconds,
        )

    @property
    def scenario_faults_enabled(self) -> bool:
        """Whether armed faults are consulted. Never in production (ADR D11)."""
        return not self.profile.is_production

    def __repr__(self) -> str:
        """Redacted. The credentials are the reason this is not the default repr."""
        return (
            f"WorkerSettings(profile={self.profile.value}, worker_id={self.worker_id!r}, "
            f"batch_size={self.batch_size}, lease_seconds={self.lease_seconds})"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings(**overrides: Any) -> WorkerSettings:
    """Process settings, read from the environment once. Tests construct directly."""
    return WorkerSettings(**overrides)


# --------------------------------------------------------------------------- engines
#
# Built from the settings URLs rather than through platform_db.session_scope(WORKER),
# for the reason Unit A recorded for the API: platform_db.database_url(role) looks up
# DATABASE_URL_<ROLE.upper()> -- literally DATABASE_URL_COMMERCE_WORKER -- and otherwise
# falls back to a shared DATABASE_URL. With the ADR-mandated names that fallback would
# always win, and every kernel call would run under whatever privileges DATABASE_URL
# happens to carry. session_scope_for reproduces platform_db.session_scope's contract
# exactly against an explicit URL.


@lru_cache(maxsize=4)
def engine_for(url: str) -> Engine:
    """One pooled engine per URL. Cached so a tick does not build a new pool per tenant.

    The pool is deliberately small. A worker runs one command at a time and never holds a
    worker-role and a kernel-role transaction open together except in the fault path, so
    the pool exists to *reuse* a connection rather than to run several. A large pool here
    would take connection slots from the API for no throughput at all -- and PostgreSQL
    runs out of slots long before this process runs out of work.
    """
    return create_engine(url, future=True, pool_pre_ping=True, pool_size=2, max_overflow=3)


@contextmanager
def session_scope_for(url: str) -> Iterator[Session]:
    """A transaction that commits on a clean exit and rolls back on any exception."""
    session = Session(engine_for(url), expire_on_commit=False, future=True)
    try:
        with session.begin():
            yield session
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """The assembled process: two database identities, one provider transport, one config.

    Handlers take this as their first argument, so a test substitutes a scripted
    transport and a settings object built from a dictionary, and nothing else changes.
    """

    settings: WorkerSettings
    razorpay: RazorpayConfig
    transport: HttpTransport

    def worker_session(self) -> AbstractContextManager[Session]:
        """A ``commerce_worker`` transaction: outbox, webhook inbox, scenario faults."""
        return session_scope_for(self.settings.database_url_worker)

    def kernel_session(self) -> AbstractContextManager[Session]:
        """A ``commerce_kernel`` transaction: kernel calls, and nothing else."""
        return session_scope_for(self.settings.database_url_kernel)

    @property
    def retry_policy(self) -> RetryPolicy:
        return self.settings.retry_policy()


def build_runtime(
    settings: WorkerSettings, *, transport: HttpTransport | None = None
) -> WorkerRuntime:
    """Assemble the runtime. ``transport`` is injected by tests and by nothing else."""
    return WorkerRuntime(
        settings=settings,
        razorpay=settings.razorpay(),
        transport=transport
        if transport is not None
        else HttpxTransport(timeout_seconds=settings.provider_timeout_seconds),
    )

"""Loading Razorpay configuration from the environment, specification 11.5.

Every test hands :func:`load_config_from_env` a plain ``dict``. Nothing here reads or
writes ``os.environ``, so the suite cannot pick up a developer's real ``.env`` and cannot
be affected by ``RAZORPAY_*`` being set on the machine that runs it.
"""

from __future__ import annotations

import pytest
from payment_adapters.razorpay import (
    API_CREDENTIAL_VARIABLE,
    KEY_ID_VARIABLE,
    PRODUCTION_APPROVAL_VARIABLE,
    PROFILE_VARIABLE,
    REQUIRED_VARIABLES,
    WEBHOOK_CREDENTIAL_VARIABLE,
    ConfigurationError,
    RazorpayProfile,
    load_config_from_env,
)

from conftest import API_KEY_MATERIAL, LIVE_KEY_ID, TEST_KEY_ID, WEBHOOK_KEY_MATERIAL

APPROVAL_REF = "CHG-2026-0043/live-enablement"


def environ(**overrides: str) -> dict[str, str]:
    """A complete, valid development environment, with overrides applied."""
    base = {
        KEY_ID_VARIABLE: TEST_KEY_ID,
        API_CREDENTIAL_VARIABLE: API_KEY_MATERIAL,
        WEBHOOK_CREDENTIAL_VARIABLE: WEBHOOK_KEY_MATERIAL,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------- loading


def test_a_complete_environment_loads_a_test_mode_config() -> None:
    cfg = load_config_from_env(environ())
    assert cfg.key_id == TEST_KEY_ID
    assert cfg.key_secret == API_KEY_MATERIAL
    assert cfg.webhook_secret == WEBHOOK_KEY_MATERIAL
    assert cfg.is_test_mode


def test_the_profile_defaults_to_development() -> None:
    """The most restrictive profile is the default, so forgetting it cannot loosen anything."""
    assert PROFILE_VARIABLE not in environ()
    assert load_config_from_env(environ()).profile is RazorpayProfile.DEVELOPMENT


@pytest.mark.parametrize("raw", ["DEMO", "demo", " Demo "])
def test_the_profile_is_read_case_insensitively(raw: str) -> None:
    cfg = load_config_from_env(environ(**{PROFILE_VARIABLE: raw}))
    assert cfg.profile is RazorpayProfile.DEMO


def test_an_unrecognised_profile_is_refused() -> None:
    with pytest.raises(ConfigurationError, match=PROFILE_VARIABLE):
        load_config_from_env(environ(**{PROFILE_VARIABLE: "STAGING"}))


def test_values_are_stripped_of_surrounding_whitespace() -> None:
    """A trailing newline from a hand-edited ``.env`` must not become part of an HMAC key."""
    cfg = load_config_from_env(
        environ(
            **{
                KEY_ID_VARIABLE: f" {TEST_KEY_ID}\n",
                API_CREDENTIAL_VARIABLE: f"{API_KEY_MATERIAL}\n",
                WEBHOOK_CREDENTIAL_VARIABLE: f"\t{WEBHOOK_KEY_MATERIAL}",
            }
        )
    )
    assert cfg.key_id == TEST_KEY_ID
    assert cfg.key_secret == API_KEY_MATERIAL
    assert cfg.webhook_secret == WEBHOOK_KEY_MATERIAL


def test_the_mapping_is_not_mutated() -> None:
    env = environ()
    before = dict(env)
    load_config_from_env(env)
    assert env == before


# ---------------------------------------------------------------------- the guard


@pytest.mark.parametrize("profile", ["DEVELOPMENT", "DEMO", None])
def test_a_live_key_in_a_test_only_profile_refuses_to_start(profile: str | None) -> None:
    """The guard, reached through the environment path rather than direct construction."""
    env = environ(**{KEY_ID_VARIABLE: LIVE_KEY_ID})
    if profile is not None:
        env[PROFILE_VARIABLE] = profile
    with pytest.raises(ConfigurationError, match="accepts only"):
        load_config_from_env(env)


def test_a_live_key_in_production_needs_the_recorded_approval() -> None:
    env = environ(**{KEY_ID_VARIABLE: LIVE_KEY_ID, PROFILE_VARIABLE: "PRODUCTION"})
    with pytest.raises(ConfigurationError, match="production_approval_ref"):
        load_config_from_env(env)

    env[PRODUCTION_APPROVAL_VARIABLE] = APPROVAL_REF
    cfg = load_config_from_env(env)
    assert not cfg.is_test_mode
    assert cfg.production_approval_ref == APPROVAL_REF


def test_a_blank_approval_reference_is_not_an_approval() -> None:
    env = environ(
        **{
            KEY_ID_VARIABLE: LIVE_KEY_ID,
            PROFILE_VARIABLE: "PRODUCTION",
            PRODUCTION_APPROVAL_VARIABLE: "   ",
        }
    )
    with pytest.raises(ConfigurationError, match="production_approval_ref"):
        load_config_from_env(env)


def test_a_test_key_in_production_is_refused() -> None:
    env = environ(**{PROFILE_VARIABLE: "PRODUCTION", PRODUCTION_APPROVAL_VARIABLE: APPROVAL_REF})
    with pytest.raises(ConfigurationError, match="rejects"):
        load_config_from_env(env)


def test_shared_secret_material_is_refused_through_the_environment_path() -> None:
    with pytest.raises(ConfigurationError, match="must differ"):
        load_config_from_env(environ(**{WEBHOOK_CREDENTIAL_VARIABLE: API_KEY_MATERIAL}))


# ------------------------------------------------------------------ missing variables


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
def test_a_missing_variable_is_named_and_nothing_else_is(missing: str) -> None:
    """The message says which variable to set, and carries no value of any variable."""
    env = environ()
    del env[missing]
    with pytest.raises(ConfigurationError) as excinfo:
        load_config_from_env(env)
    message = str(excinfo.value)
    assert missing in message
    for name, value in env.items():
        assert value not in message, name
    assert "rzp_" not in message


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_variable_counts_as_missing(missing: str, blank: str) -> None:
    with pytest.raises(ConfigurationError, match=missing):
        load_config_from_env(environ(**{missing: blank}))


def test_an_empty_environment_names_the_first_missing_variable() -> None:
    with pytest.raises(ConfigurationError, match=KEY_ID_VARIABLE):
        load_config_from_env({})


def test_the_required_set_is_exactly_the_three_credentials() -> None:
    """The profile is optional by design; the three credentials never are."""
    assert set(REQUIRED_VARIABLES) == {
        KEY_ID_VARIABLE,
        API_CREDENTIAL_VARIABLE,
        WEBHOOK_CREDENTIAL_VARIABLE,
    }
    assert PROFILE_VARIABLE not in REQUIRED_VARIABLES


# ------------------------------------------------------------------------- leakage


def test_the_loaded_config_never_renders_secret_material() -> None:
    """Startup logs the config; the rendered form must be safe to log."""
    cfg = load_config_from_env(environ())
    rendered = f"{cfg!r} {cfg}"
    assert API_KEY_MATERIAL not in rendered
    assert WEBHOOK_KEY_MATERIAL not in rendered
    assert TEST_KEY_ID not in rendered
    assert "test_mode=True" in rendered


def test_a_guard_failure_never_renders_secret_material() -> None:
    """The exception a misconfigured process dies with ends up in a log too."""
    env = environ(**{KEY_ID_VARIABLE: LIVE_KEY_ID})
    with pytest.raises(ConfigurationError) as excinfo:
        load_config_from_env(env)
    message = str(excinfo.value)
    assert API_KEY_MATERIAL not in message
    assert WEBHOOK_KEY_MATERIAL not in message
    assert LIVE_KEY_ID not in message

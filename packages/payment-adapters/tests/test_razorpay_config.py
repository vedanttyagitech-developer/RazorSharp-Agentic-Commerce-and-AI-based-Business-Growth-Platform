"""The guard that stops a live key reaching a demo, specification 11.5.

Every test here corresponds to a way somebody has actually shipped a live key by
accident: pasting one into a dev ``.env``, flipping a single boolean to unblock a
pipeline, or reusing the API secret as the webhook secret because both were "the
Razorpay password".
"""

from __future__ import annotations

import dataclasses

import pytest
from payment_adapters.razorpay import ConfigurationError, RazorpayConfig, RazorpayProfile

from conftest import API_KEY_MATERIAL, LIVE_KEY_ID, TEST_KEY_ID, WEBHOOK_KEY_MATERIAL

APPROVAL_REF = "CHG-2026-0043/live-enablement"


def load(**overrides: object) -> RazorpayConfig:
    kwargs: dict[str, object] = {
        "key_id": TEST_KEY_ID,
        "key_secret": API_KEY_MATERIAL,
        "webhook_secret": WEBHOOK_KEY_MATERIAL,
        "profile": RazorpayProfile.DEVELOPMENT,
    }
    kwargs.update(overrides)
    return RazorpayConfig.load(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the two branches


def test_development_profile_accepts_a_test_key() -> None:
    cfg = load()
    assert cfg.is_test_mode
    assert cfg.profile is RazorpayProfile.DEVELOPMENT


def test_development_profile_rejects_a_live_key() -> None:
    """The guard. A live key in a demo charges a real card for a scripted basket."""
    with pytest.raises(ConfigurationError, match="accepts only"):
        load(key_id=LIVE_KEY_ID)


def test_demo_profile_rejects_a_live_key() -> None:
    with pytest.raises(ConfigurationError, match="accepts only"):
        load(key_id=LIVE_KEY_ID, profile=RazorpayProfile.DEMO)


def test_production_profile_accepts_a_live_key_with_a_recorded_approval() -> None:
    """The other branch: live credentials are possible, but only deliberately."""
    cfg = load(
        key_id=LIVE_KEY_ID,
        profile=RazorpayProfile.PRODUCTION,
        production_approval_ref=APPROVAL_REF,
    )
    assert not cfg.is_test_mode
    assert cfg.production_approval_ref == APPROVAL_REF


def test_production_profile_alone_is_not_enough_for_a_live_key() -> None:
    """Specification 11.5 requires a profile *and* a separate approval.

    A single boolean is exactly the kind of thing that gets flipped at 2am to unblock a
    deploy, so the second fact must be a value somebody had to look up.
    """
    with pytest.raises(ConfigurationError, match="production_approval_ref"):
        load(key_id=LIVE_KEY_ID, profile=RazorpayProfile.PRODUCTION)


def test_production_profile_with_a_blank_approval_is_not_enough() -> None:
    with pytest.raises(ConfigurationError, match="production_approval_ref"):
        load(key_id=LIVE_KEY_ID, profile=RazorpayProfile.PRODUCTION, production_approval_ref="")


# ------------------------------------------------------------------- the other way


def test_production_profile_rejects_a_test_key() -> None:
    """A production deployment in test mode reports success while taking no money."""
    with pytest.raises(ConfigurationError, match="rejects"):
        load(profile=RazorpayProfile.PRODUCTION, production_approval_ref=APPROVAL_REF)


def test_unrecognised_key_prefix_is_refused_not_assumed_to_be_test() -> None:
    """An unknown future prefix must not be waved through as harmless."""
    with pytest.raises(ConfigurationError, match="unrecognised key prefix"):
        load(key_id="rzp_sandbox_1DP5mmOlF5G5ag")


@pytest.mark.parametrize("key_id", ["", "RZP_TEST_UPPER", " rzp_test_leading_space"])
def test_malformed_key_ids_are_refused(key_id: str) -> None:
    with pytest.raises(ConfigurationError):
        load(key_id=key_id)


# ---------------------------------------------------------------- secret separation


def test_webhook_secret_must_differ_from_the_api_secret() -> None:
    """Sharing them turns a webhook-secret leak into full payment authority."""
    with pytest.raises(ConfigurationError, match="must differ"):
        load(webhook_secret=API_KEY_MATERIAL)


@pytest.mark.parametrize("field_name", ["key_secret", "webhook_secret"])
@pytest.mark.parametrize("value", ["", "short"])
def test_empty_or_stub_secrets_are_refused(field_name: str, value: str) -> None:
    """An empty HMAC key is shared, not weak: every candidate signature verifies."""
    with pytest.raises(ConfigurationError):
        load(**{field_name: value})


# ------------------------------------------------------------------------- leakage


def test_repr_never_discloses_secret_material() -> None:
    """A config reaches a log through a traceback far more often than on purpose."""
    cfg = load()
    rendered = f"{cfg!r} {cfg}"
    assert API_KEY_MATERIAL not in rendered
    assert WEBHOOK_KEY_MATERIAL not in rendered
    assert TEST_KEY_ID not in rendered
    # It must still be useful for debugging: mode is the fact an operator needs.
    assert "test_mode=True" in rendered


def test_replace_cannot_smuggle_a_live_key_past_the_guard() -> None:
    """Validation lives in ``__post_init__``, so every construction path re-checks it."""
    cfg = load()
    with pytest.raises(ConfigurationError):
        dataclasses.replace(cfg, key_id=LIVE_KEY_ID)


# ----------------------------------------------------------------------------- urls


def test_receipt_is_percent_encoded_in_the_lookup_url() -> None:
    """The recovery lookup must survive a receipt containing URL metacharacters.

    An unencoded ``&`` would truncate the query and return the wrong order, which in the
    one situation this call exists for -- a lost create-order response -- is how a second
    order gets created for a checkout that already has one.
    """
    cfg = load()
    url = cfg.order_lookup_url("chk/7f3a&v=1")
    assert "chk%2F7f3a%26v%3D1" in url
    assert "chk/7f3a&v=1" not in url

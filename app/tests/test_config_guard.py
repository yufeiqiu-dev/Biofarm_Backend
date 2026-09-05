"""The production guardrails in Settings.

These matter more than most tests here: the failure they prevent has no visible
symptom. An app booted with AUTH_BYPASS on serves the entire admin API to
anyone who finds the URL while looking completely healthy, and one booted with
STRIPE_BYPASS on creates orders without charging. Refusing to start is the
whole feature, so it needs to actually refuse.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# Every field Settings requires, so each test varies only what it is about.
BASE = {
    "database_url": "postgresql+psycopg://u:p@localhost:5432/oasis",
    "cognito_region": "us-east-2",
    "cognito_user_pool_id": "us-east-2_example",
    "s3_bucket_name": "bucket",
    "aws_region": "us-east-2",
    "cloudfront_url": "https://example.cloudfront.net",
    "aws_access_key_id": "key",
    "aws_secret_access_key": "secret",
}

SAFE_PROD = {
    **BASE,
    "app_env": "prod",
    "auth_bypass": False,
    "stripe_bypass": False,
    "stripe_secret_key": "sk_test_x",
    "stripe_webhook_secret": "whsec_x",
    "cognito_user_pool_client_id": "1h2j3k4l5m6n7o8p9q0r",
}


def test_development_allows_bypasses():
    settings = Settings(**BASE, app_env="dev", auth_bypass=True, stripe_bypass=True)
    assert settings.auth_bypass is True
    assert settings.stripe_bypass is True


def test_correctly_configured_production_boots():
    settings = Settings(**SAFE_PROD)
    assert settings.auth_bypass is False
    assert settings.stripe_bypass is False


def test_production_refuses_auth_bypass():
    with pytest.raises(ValidationError, match="AUTH_BYPASS"):
        Settings(**{**SAFE_PROD, "auth_bypass": True})


def test_production_refuses_stripe_bypass():
    with pytest.raises(ValidationError, match="STRIPE_BYPASS"):
        Settings(**{**SAFE_PROD, "stripe_bypass": True})


def test_production_requires_stripe_keys():
    with pytest.raises(ValidationError, match="STRIPE_SECRET_KEY"):
        Settings(**{**SAFE_PROD, "stripe_secret_key": ""})

    # The webhook is the only thing that creates orders in live mode, so a
    # missing signing secret means every payment silently fails to become one.
    with pytest.raises(ValidationError, match="STRIPE_WEBHOOK_SECRET"):
        Settings(**{**SAFE_PROD, "stripe_webhook_secret": ""})


def test_production_requires_the_app_client_id():
    """Without it, _verify_access_token skips the client_id check and accepts a
    token issued to any app client in the pool - including one added later for
    something else entirely."""
    with pytest.raises(ValidationError, match="COGNITO_USER_POOL_CLIENT_ID"):
        Settings(**{**SAFE_PROD, "cognito_user_pool_client_id": ""})


def test_development_does_not_require_the_app_client_id():
    """A local .env written before this check existed must still boot."""
    settings = Settings(**BASE, app_env="dev", cognito_user_pool_client_id="")
    assert settings.cognito_user_pool_client_id == ""


@pytest.mark.parametrize("env", ["prod", "PROD", "production", "Production"])
def test_guard_is_case_insensitive(env):
    """APP_ENV is free text, so 'Production' must not slip past the check."""
    with pytest.raises(ValidationError, match="AUTH_BYPASS"):
        Settings(**{**SAFE_PROD, "app_env": env, "auth_bypass": True})


def test_all_problems_reported_together():
    """One boot, one complete list - not a fix-and-rediscover loop."""
    with pytest.raises(ValidationError) as exc:
        Settings(**{**SAFE_PROD, "auth_bypass": True, "stripe_bypass": True})
    message = str(exc.value)
    assert "AUTH_BYPASS" in message
    assert "STRIPE_BYPASS" in message


# --- Stripe mode ---
#
# Both mixups below leave an application that starts, serves traffic and looks
# entirely healthy. Only the money is wrong.

TEST_MODE = {**BASE, "stripe_bypass": False, "stripe_mode": "test", "stripe_secret_key": "sk_test_abc"}
LIVE_MODE = {**SAFE_PROD, "stripe_mode": "live", "stripe_secret_key": "sk_live_abc"}


def test_matching_test_mode_boots():
    assert Settings(**TEST_MODE).stripe_mode == "test"


def test_matching_live_mode_boots():
    assert Settings(**LIVE_MODE).stripe_mode == "live"


def test_a_live_key_in_a_test_environment_is_refused():
    """Staging with live keys charges real cards for every test order, and
    nothing on the application side looks any different."""
    with pytest.raises(ValidationError, match="live key"):
        Settings(**{**TEST_MODE, "stripe_secret_key": "sk_live_abc"})


def test_a_test_key_in_production_is_refused():
    """The worse of the two: every order succeeds and no money is ever taken,
    silently, until someone reconciles orders against the Stripe ledger."""
    with pytest.raises(ValidationError, match="test key"):
        Settings(**{**LIVE_MODE, "stripe_secret_key": "sk_test_abc"})


def test_an_unrecognised_key_is_refused():
    with pytest.raises(ValidationError, match="neither test nor live"):
        Settings(**{**TEST_MODE, "stripe_secret_key": "rk_live_restricted"})


def test_an_invalid_mode_is_refused():
    with pytest.raises(ValidationError, match="STRIPE_MODE"):
        Settings(**{**TEST_MODE, "stripe_mode": "sandbox"})


def test_the_mode_is_case_insensitive():
    """APP_ENV already had this problem; no reason to repeat it here."""
    assert Settings(**{**LIVE_MODE, "stripe_mode": "LIVE"})


def test_bypass_skips_the_check_entirely():
    """No key is used at all under bypass, so there is nothing to be consistent
    with - and local development should not have to declare a mode."""
    settings = Settings(**BASE, stripe_bypass=True, stripe_mode="live", stripe_secret_key="")
    assert settings.stripe_bypass is True


def test_an_absent_key_is_left_to_the_production_guard():
    """Empty is a different failure with a different message; this validator
    should not pre-empt it and report the wrong problem."""
    with pytest.raises(ValidationError, match="STRIPE_SECRET_KEY is required"):
        Settings(**{**LIVE_MODE, "stripe_secret_key": ""})

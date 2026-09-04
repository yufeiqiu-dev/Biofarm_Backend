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

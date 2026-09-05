"""How the app authenticates to AWS.

This exists because the App Runner stack supplies no access key - the container
has an instance role instead - while these settings were required, so the
service could not start at all. It failed Settings validation, exited, and never
passed a health check. Nothing caught it because nothing had been deployed.
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core import aws
from app.core.config import Settings, get_settings

BASE = {
    "database_url": "postgresql+psycopg://u:p@localhost:5432/oasis",
    "cognito_region": "us-east-2",
    "cognito_user_pool_id": "us-east-2_example",
    "cognito_user_pool_client_id": "1h2j3k4l5m6n7o8p9q0r",
    "s3_bucket_name": "bucket",
    "aws_region": "us-east-2",
    "cloudfront_url": "https://example.cloudfront.net",
    "app_env": "prod",
    "stripe_secret_key": "sk_live_x",
    "stripe_mode": "live",
    "stripe_webhook_secret": "whsec_x",
    "email_from": "orders@example.com",
    # Explicit, because conftest bypasses email for the suite and APP_ENV=prod
    # refuses to boot with that on.
    "email_bypass": False,
}


def test_it_boots_with_no_access_key_at_all(monkeypatch):
    """Exactly what App Runner provides. If this fails, the deploy comes up
    unhealthy and the logs say "field required" rather than anything about
    roles.

    The environment is cleared explicitly: conftest pins a key for the rest of
    the suite, and inheriting it here would make this pass for the wrong reason.
    """
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    settings = Settings(**BASE, _env_file=None)

    assert settings.aws_access_key_id == ""


@pytest.fixture
def clean_cache():
    get_settings.cache_clear()
    aws.get_client.cache_clear()
    yield
    get_settings.cache_clear()
    aws.get_client.cache_clear()


def test_without_keys_boto3_is_left_to_find_the_role(clean_cache, monkeypatch):
    """Passing an empty credential is not the same as passing none: boto3 would
    take the empty string as the answer and never consult the instance role."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "")

    with patch.object(aws.boto3, "client") as client:
        aws.get_client("ses")

    kwargs = client.call_args.kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    assert kwargs["region_name"] == "us-east-2"


def test_with_keys_they_are_used(clean_cache, monkeypatch):
    """A laptop has no role to assume, so the .env key still has to work."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")

    with patch.object(aws.boto3, "client") as client:
        aws.get_client("s3")

    kwargs = client.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert kwargs["aws_secret_access_key"] == "shhh"


def test_production_still_refuses_the_things_that_matter(clean_cache):
    """Relaxing the credentials must not have relaxed the guards around them."""
    with pytest.raises(ValidationError, match="AUTH_BYPASS"):
        Settings(**{**BASE, "auth_bypass": True}, _env_file=None)

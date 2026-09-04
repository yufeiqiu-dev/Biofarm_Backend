from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENVS = {"prod", "production"}


class Settings(BaseSettings):
    app_name: str = "oasis-backend"
    app_env: str = "dev"
    api_v1_prefix: str = "/api/v1"

    # Override in production with the deployed frontend origin
    cors_origins: list[str] = ["http://localhost:5174"]

    # Set to True in local dev to skip Cognito verification
    auth_bypass: bool = False

    # Required — must be set in .env or environment
    database_url: str
    cognito_region: str
    cognito_user_pool_id: str
    s3_bucket_name: str
    aws_region: str
    cloudfront_url: str  # e.g. https://d1234abcd.cloudfront.net
    aws_access_key_id: str
    aws_secret_access_key: str

    # The app client access tokens must have been issued to. Optional so a local
    # .env predating this check still boots; required under APP_ENV=prod by the
    # validator below, because without it any app client in the pool is accepted.
    cognito_user_pool_client_id: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_bypass: bool = False  # When True, skip real Stripe API calls (dev/test mode)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _refuse_unsafe_production_config(self) -> "Settings":
        """Refuse to start a production app with development shortcuts enabled.

        Both bypasses are catastrophic in production and neither has a visible
        symptom: with auth_bypass, every request arriving without an
        Authorization header is treated as an admin, so the whole admin API is
        open to anyone who finds the URL; with stripe_bypass, checkout creates
        orders inline without ever charging a card. A copied .env or a stale
        task definition is all it takes, and the app would otherwise come up
        looking perfectly healthy.

        Failing to boot is the point. A deployment that will not start gets
        noticed within minutes; one that starts wide open does not.
        """
        if self.app_env.lower() not in PRODUCTION_ENVS:
            return self

        problems = []
        if self.auth_bypass:
            problems.append("AUTH_BYPASS must be false (it makes every unauthenticated request an admin)")
        if self.stripe_bypass:
            problems.append("STRIPE_BYPASS must be false (it creates orders without charging)")
        if not self.stripe_secret_key:
            problems.append("STRIPE_SECRET_KEY is required")
        if not self.stripe_webhook_secret:
            problems.append("STRIPE_WEBHOOK_SECRET is required (orders are created by the webhook)")
        if not self.cognito_user_pool_client_id:
            problems.append(
                "COGNITO_USER_POOL_CLIENT_ID is required "
                "(without it, a token from any app client in the pool is accepted)"
            )

        if problems:
            raise ValueError(
                f"Unsafe configuration for APP_ENV={self.app_env!r}: " + "; ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENVS = {"prod", "production"}

# Stripe issues keys per mode, and the prefix is the only thing that says which
# is which. STRIPE_MODE declares the mode the deployment is *supposed* to be in,
# so a key from the other one is a startup failure rather than a discovery.
STRIPE_MODES = {"test": "sk_test_", "live": "sk_live_"}


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

    # Which Stripe mode this deployment expects: "test" or "live". Staging runs
    # "test" against real Stripe, so the webhook, manual capture and refund paths
    # are genuinely exercised without money moving. See _refuse_mismatched_stripe_mode.
    stripe_mode: str = "test"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _refuse_mismatched_stripe_mode(self) -> "Settings":
        """Refuse to start when the Stripe key does not match the declared mode.

        Two mistakes this exists to stop, both of which look completely healthy:

        Live keys in staging. Every test order charges a real card. Nobody
        notices from the application side, because the code path is identical -
        the difference is only which of Stripe's ledgers it lands in.

        Test keys in production. Every order succeeds and no money is ever taken.
        This one is worse, because the failure is silent for as long as nobody
        reconciles the books against the orders.

        The key prefix is the only thing distinguishing them, and both are
        pasted in by hand at deploy time. STRIPE_MODE is set by the
        infrastructure alongside the secret, so the two have to agree.

        Not checked under stripe_bypass, where no key is used at all.
        """
        if self.stripe_bypass:
            return self

        mode = self.stripe_mode.lower()
        if mode not in STRIPE_MODES:
            raise ValueError(
                f"STRIPE_MODE must be one of {sorted(STRIPE_MODES)}, got {self.stripe_mode!r}"
            )

        # An empty key is the concern of the production guard below, which knows
        # whether one is required here at all.
        if self.stripe_secret_key and not self.stripe_secret_key.startswith(STRIPE_MODES[mode]):
            actual = next(
                (name for name, prefix in STRIPE_MODES.items()
                 if self.stripe_secret_key.startswith(prefix)),
                "neither test nor live",
            )
            raise ValueError(
                f"STRIPE_MODE is {mode!r} but STRIPE_SECRET_KEY is a {actual} key. "
                f"A live key in a test environment charges real cards; a test key in "
                f"production takes no money at all."
            )

        return self

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

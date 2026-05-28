from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

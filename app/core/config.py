from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "oasis-backend"
    app_env: str = "dev"
    api_v1_prefix: str = "/api/v1"

    # For local dev, your React app is probably on Vite:
    cors_origins: list[str] = ["http://localhost:5174"]

    # Set to True in local dev to skip Cognito verification
    auth_bypass: bool = False

    cognito_region: str = "us-east-2"
    cognito_user_pool_id: str = ""

    database_url: str = "postgresql+psycopg://postgres:@localhost:5432/oasis"


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
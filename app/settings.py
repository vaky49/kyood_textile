from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://procurement:procurement@localhost:5432/procurement"
    frontend_origins: str = "http://localhost:3000"
    operator_api_token: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_api_version: str = "v24.0"
    messaging_provider: str = "mock"
    llm_provider: str = "mock"
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    media_storage_dir: str = "media"


@lru_cache
def get_settings() -> Settings:
    return Settings()
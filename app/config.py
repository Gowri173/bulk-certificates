"""Application configuration loaded from environment variables and .env file."""

from pathlib import Path
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for bulk certificate generator service."""

    DATABASE_URL: str = "sqlite:///./certificates.db"
    STORAGE_DIR: str = "./storage"
    MAX_RECIPIENTS: int = 5000
    PROCESSING_MODE: Literal["background", "inline"] = "background"
    WORKER_THREADS: int = 2
    BATCH_SIZE: int = 50
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def storage_path(self) -> Path:
        """Return the resolved Path instance for STORAGE_DIR."""
        return Path(self.STORAGE_DIR).resolve()


_settings_override: Optional[Settings] = None


def get_settings() -> Settings:
    """Return active settings instance or create default from environment."""
    if _settings_override is not None:
        return _settings_override
    return Settings()


def set_settings(settings: Optional[Settings]) -> None:
    """Override settings instance globally (used in test suite)."""
    global _settings_override
    _settings_override = settings

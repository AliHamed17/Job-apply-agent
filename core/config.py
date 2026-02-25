"""Core configuration — loads settings from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, populated from env vars / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Environment ──────────────────────────────────────
    app_env: Literal["dev", "test", "prod"] = "dev"

    # ── WhatsApp Cloud API ──────────────────────────────
    whatsapp_verify_token: str = ""
    whatsapp_api_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""

    # ── Database ────────────────────────────────────────
    database_url: str = "sqlite:///./job_agent.db"

    # ── Redis ───────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── LLM ─────────────────────────────────────────────
    llm_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "gpt-4o"

    # ── Application Modes ───────────────────────────────
    draft_only: bool = True
    auto_apply: bool = False

    # ── Rate Limiting ───────────────────────────────────
    rate_limit_requests_per_minute: int = 10
    polite_crawl_delay_seconds: float = 2.0

    # ── Security ────────────────────────────────────────
    secret_key: str = "change-me"

    # ── Allowed Senders ─────────────────────────────────
    allowed_senders: str = ""  # comma-separated phone numbers

    # ── Paths ───────────────────────────────────────────
    user_profile_path: str = "user_profile.yaml"

    # ── Derived helpers ─────────────────────────────────
    @property
    def allowed_sender_list(self) -> list[str]:
        if not self.allowed_senders:
            return []
        return [s.strip() for s in self.allowed_senders.split(",") if s.strip()]

    @property
    def db_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def profile_path(self) -> Path:
        return Path(self.user_profile_path)

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    def validate_runtime_config(self) -> list[str]:
        """Validate critical runtime settings and return errors, if any."""
        errors: list[str] = []

        if self.is_production and self.secret_key == "change-me":
            errors.append("SECRET_KEY must be set to a secure random value in production")

        if self.is_production and self.whatsapp_api_token and not self.whatsapp_app_secret:
            errors.append(
                "WHATSAPP_APP_SECRET must be set in production "
                "when WHATSAPP_API_TOKEN is configured"
            )

        return errors


@lru_cache
def get_settings() -> Settings:
    """Singleton accessor for application settings."""
    return Settings()

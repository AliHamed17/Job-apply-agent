"""Configuration owned by the discovery bounded context."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscoveryMeshSettings(BaseSettings):
    """Validated discovery settings kept outside inference attestation scope."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    public_discovery_enabled: bool = True
    public_discovery_interval_h: int = Field(default=6, ge=6, le=168)
    public_discovery_max_jobs: int = Field(default=50, ge=1, le=500)
    public_discovery_timeout_s: float = Field(default=20.0, ge=1.0, le=120.0)
    tasks_always_eager: bool = True

    discovery_scheduler_interval_seconds: int = Field(default=60, ge=60, le=600)
    discovery_poll_interval_seconds: int = Field(default=600, ge=600, le=3600)
    discovery_http_max_attempts: int = Field(default=3, ge=1, le=5)
    discovery_http_max_response_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    discovery_max_pages_per_run: int = Field(default=100, ge=1, le=100)
    discovery_stale_run_seconds: int = Field(default=1800, ge=300, le=86_400)
    employer_catalog_path: str = "employer_catalog.yaml"
    gmail_alert_enabled: bool = False
    gmail_alert_label: str = Field(default="JobApplyAgent", min_length=1, max_length=128)
    gmail_oauth_token_path: str = ".gmail_oauth.json"
    gmail_alert_max_messages: int = Field(default=100, ge=1, le=500)


@lru_cache
def get_discovery_settings() -> DiscoveryMeshSettings:
    return DiscoveryMeshSettings()

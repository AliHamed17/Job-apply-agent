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

    # ── Environment ─────────────────────────────────────
    app_env: Literal["development", "test", "production"] = "development"

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
    llm_provider: Literal["openai", "anthropic", "ollama", "mock"] = "openai"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "gpt-4o"
    ollama_base_url: str = "http://localhost:11434"
    llm_cv_routing: bool = True
    llm_cv_alignment: bool = True

    # ── Application Modes ───────────────────────────────
    draft_only: bool = True
    auto_apply: bool = False
    auto_apply_threshold: float = 80.0
    tasks_always_eager: bool = True  # If True, runs tasks synchronously (no Redis needed)

    # ── Rate Limiting ───────────────────────────────────
    rate_limit_requests_per_minute: int = 10
    polite_crawl_delay_seconds: float = 2.0

    # ── Security ────────────────────────────────────────
    secret_key: str = "change-me"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    trusted_proxies: str = ""
    live_automation_acknowledged: bool = False
    dependency_heartbeat_ttl_seconds: int = 120

    # ── Allowed Senders ─────────────────────────────────
    allowed_senders: str = ""  # comma-separated phone numbers
    notification_recipient_email: str = ""
    notification_recipient_phone: str = ""

    # ── Job Board API Keys ───────────────────────────────
    greenhouse_api_key: str = ""
    lever_api_key: str = ""
    smartrecruiters_api_key: str = ""  # optional — public postings work without it

    # ── Browser-automation credentials (LinkedIn / Indeed) ──
    # Option A — cookie file (JSON export from browser, recommended)
    linkedin_cookies_file: str = ""
    indeed_cookies_file: str = ""
    # Option B — email + password (triggers auto-login)
    linkedin_email: str = ""
    linkedin_password: str = ""
    indeed_email: str = ""
    indeed_password: str = ""

    # ── Paths ───────────────────────────────────────────
    user_profile_path: str = "user_profile.yaml"
    application_data_dir: str = "."
    max_resume_bytes: int = 10 * 1024 * 1024
    cv_routing_path: str = "cv_routing.yaml"
    cv_directory: str = "cvs"

    # ── Full-auto policy ────────────────────────────────
    min_apply_score: float = 40.0
    queue_ttl_days: int = 7

    # ── LinkedIn rate governor ──────────────────────────
    linkedin_daily_cap: int = 45
    linkedin_min_gap_s: int = 120
    linkedin_max_gap_s: int = 360
    active_hours: str = "09:00-21:00"
    linkedin_browser_profile_dir: str = ".linkedin_profile"
    dry_run: bool = False

    # ── Authenticated employer portals ──────────────────
    # Dedicated Playwright profiles are used instead of password extraction.
    portal_browser_profile_root: str = ".portal_profiles"
    portal_browser_headless: bool = True
    portal_final_submit_enabled: bool = False
    portal_reuse_last_application: bool = True
    portal_session_lock_minutes: int = 30
    employer_workflow_path: str = "employer_workflows.yaml"

    # ── Discovery ───────────────────────────────────────
    discovery_interval_h: int = 3
    discovery_pages_per_query: int = 3
    public_discovery_enabled: bool = True
    public_discovery_interval_h: int = 6
    public_discovery_max_jobs: int = 50
    public_discovery_timeout_s: float = 20.0

    # ── WhatsApp outbound + email ───────────────────────
    wa_outbound_daily_cap: int = 15
    wa_contact_dedup_days: int = 30
    bridge_send_url: str = "http://localhost:8100/send"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_addr: str = ""

    # ── Derived helpers ─────────────────────────────────
    @property
    def allowed_sender_list(self) -> list[str]:
        if not self.allowed_senders:
            return []
        return [s.strip() for s in self.allowed_senders.split(",") if s.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [proxy.strip() for proxy in self.trusted_proxies.split(",") if proxy.strip()]

    def validate_runtime(self) -> None:
        """Reject unsafe production settings before the process accepts traffic."""
        if self.app_env != "production":
            return
        errors: list[str] = []
        if self.secret_key in {"", "change-me", "change-me-to-a-random-secret"}:
            errors.append("SECRET_KEY must be a non-default value")
        if len(self.secret_key) < 32:
            errors.append("SECRET_KEY must be at least 32 characters")
        if not self.whatsapp_app_secret:
            errors.append("WHATSAPP_APP_SECRET is required for webhook signatures")
        if "*" in self.cors_origin_list:
            errors.append("CORS_ORIGINS cannot contain '*'")
        live_requested = (
            self.auto_apply
            or not self.draft_only
            or not self.dry_run
            or self.portal_final_submit_enabled
        )
        if live_requested and not self.live_automation_acknowledged:
            errors.append(
                "LIVE_AUTOMATION_ACKNOWLEDGED=true is required for non-dry-run automation"
            )
        if self.portal_final_submit_enabled and self.dry_run:
            errors.append("PORTAL_FINAL_SUBMIT_ENABLED cannot be used with DRY_RUN=true")
        if self.portal_final_submit_enabled and self.draft_only:
            errors.append("PORTAL_FINAL_SUBMIT_ENABLED cannot be used with DRAFT_ONLY=true")
        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))

    @property
    def db_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def profile_path(self) -> Path:
        return Path(self.user_profile_path)

    @property
    def data_dir(self) -> Path:
        return Path(self.application_data_dir)

    @property
    def resume_path(self) -> Path:
        return self.data_dir / "resume.pdf"

    def active_hours_range(self) -> tuple[int, int]:
        """Parse ACTIVE_HOURS 'HH:MM-HH:MM' into (start_hour, end_hour)."""
        try:
            start, end = self.active_hours.split("-")
            return int(start.split(":")[0]), int(end.split(":")[0])
        except Exception:
            return 9, 21


@lru_cache
def get_settings() -> Settings:
    """Singleton accessor for application settings."""
    return Settings()

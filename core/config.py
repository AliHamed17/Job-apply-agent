"""Core configuration — loads settings from environment variables."""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

JOB_AGENT_ENV_FILE = "JOB_AGENT_ENV_FILE"


def is_allowed_local_ollama_endpoint(base_url: str) -> bool:
    """Allow only loopback or the explicit container-to-host gateway."""

    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return False
        normalized_host = host.rstrip(".").lower()
        if normalized_host in {"localhost", "host.docker.internal"}:
            return True
        return ipaddress.ip_address(normalized_host).is_loopback
    except (ValueError, UnicodeError):
        return False


def is_safe_production_cors_origin(origin: str) -> bool:
    """Accept exact HTTPS origins and explicit loopback HTTP development origins."""

    candidate = origin.strip()
    if not candidate or candidate.casefold() == "null" or "*" in candidate:
        return False
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        # Accessing port validates malformed and out-of-range port syntax.
        _ = parsed.port
        normalized_host = host.rstrip(".").lower()
        is_loopback = normalized_host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(normalized_host).is_loopback
            except ValueError:
                is_loopback = False
        return parsed.scheme == "https" or is_loopback
    except (ValueError, UnicodeError):
        return False


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
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ── LLM ─────────────────────────────────────────────
    llm_provider: Literal["openai", "anthropic", "ollama", "mock"] = "ollama"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    ollama_no_cloud: bool = True
    cloud_vision_enabled: bool = False
    ollama_expected_model_digest: str = Field(
        default="",
        pattern=r"^(?:|sha256:[0-9a-f]{64})$",
    )
    ollama_request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=120.0)
    ollama_connect_timeout_seconds: float = Field(default=3.0, ge=0.1, le=15.0)
    ollama_lease_wait_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    ollama_lease_ttl_seconds: int = Field(default=130, ge=5, le=300)
    ollama_num_ctx: int = Field(default=16_384, ge=8_192, le=32_768)
    ollama_circuit_failure_threshold: int = Field(default=3, ge=1, le=10)
    ollama_circuit_reset_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    llm_max_prompt_chars: int = Field(default=24_000, ge=1_000, le=100_000)
    llm_generation_max_horizon_seconds: float = Field(
        default=120.0,
        ge=1.0,
        le=120.0,
    )
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
    form_plan_ttl_minutes: int = 30
    submit_permit_ttl_seconds: int = 300
    submission_command_claim_ttl_seconds: int = 900
    submission_command_drain_interval_seconds: int = 15
    submission_command_drain_batch_size: int = 25

    # ── Discovery ───────────────────────────────────────
    discovery_enabled: bool = True
    discovery_interval_h: int = 3
    discovery_pages_per_query: int = 3
    public_discovery_enabled: bool = True
    public_discovery_interval_h: int = 6
    public_discovery_max_jobs: int = 50
    public_discovery_timeout_s: float = 20.0
    preparation_requeue_batch_size: int = Field(default=25, ge=1, le=100)

    # ── WhatsApp outbound + email ───────────────────────
    wa_outbound_daily_cap: int = 15
    wa_contact_dedup_days: int = 30
    bridge_send_url: str = "http://localhost:8100/send"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_addr: str = ""

    @model_validator(mode="after")
    def validate_local_inference_timing(self) -> Settings:
        """Keep the distributed lease valid for the complete caller horizon."""

        if self.ollama_request_timeout_seconds > self.llm_generation_max_horizon_seconds:
            raise ValueError("Ollama request timeout exceeds the generation horizon")
        if self.ollama_lease_ttl_seconds < self.llm_generation_max_horizon_seconds + 5:
            raise ValueError("Ollama inference lease TTL does not cover the generation horizon")
        return self

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

    @property
    def operator_auth_is_placeholder(self) -> bool:
        """Whether development may use the explicit prepare-only auth bypass."""

        return self.secret_key in {
            "",
            "change-me",
            "change-me-to-a-random-secret",
        }

    @property
    def operator_auth_configured(self) -> bool:
        """Whether bearer authentication is strong enough to authorize live send."""

        return not self.operator_auth_is_placeholder and len(self.secret_key) >= 32

    @property
    def whatsapp_app_secret_is_placeholder(self) -> bool:
        """Reject shipped examples and weak webhook-signature secrets."""

        return self.whatsapp_app_secret.strip() in {
            "",
            "change-me",
            "your-app-secret-for-signature-verification",
        }

    def validate_runtime(self) -> None:
        """Reject unsafe production settings before the process accepts traffic."""
        if self.app_env != "production":
            return
        errors: list[str] = []
        if self.operator_auth_is_placeholder:
            errors.append("SECRET_KEY must be a non-default value")
        if len(self.secret_key) < 32:
            errors.append("SECRET_KEY must be at least 32 characters")
        if self.whatsapp_app_secret_is_placeholder:
            errors.append("WHATSAPP_APP_SECRET must be a non-default value")
        if len(self.whatsapp_app_secret) < 32:
            errors.append("WHATSAPP_APP_SECRET must be at least 32 characters")
        if self.tasks_always_eager:
            errors.append("TASKS_ALWAYS_EAGER must be false in production")
        if self.llm_provider != "ollama":
            errors.append("LLM_PROVIDER must be ollama in production")
        if self.llm_model.strip() != "qwen2.5:7b":
            errors.append("LLM_MODEL must be the qualified qwen2.5:7b model")
        if not is_allowed_local_ollama_endpoint(self.ollama_base_url):
            errors.append("OLLAMA_BASE_URL must be a local inference endpoint")
        if not self.ollama_no_cloud:
            errors.append("OLLAMA_NO_CLOUD must remain enabled")
        if self.cloud_vision_enabled:
            errors.append("CLOUD_VISION_ENABLED must remain disabled in production")
        try:
            from llm.qualification_registry import (
                expected_qualified_model_digest,
                qualified_model_report_is_current,
            )

            expected_qualified_model_digest(self.ollama_expected_model_digest)
            if not qualified_model_report_is_current(
                ollama_request_timeout_seconds=self.ollama_request_timeout_seconds,
                llm_generation_max_horizon_seconds=(self.llm_generation_max_horizon_seconds),
                ollama_connect_timeout_seconds=self.ollama_connect_timeout_seconds,
                ollama_lease_wait_seconds=self.ollama_lease_wait_seconds,
                ollama_lease_ttl_seconds=self.ollama_lease_ttl_seconds,
                ollama_circuit_failure_threshold=self.ollama_circuit_failure_threshold,
                ollama_circuit_reset_seconds=self.ollama_circuit_reset_seconds,
                ollama_num_ctx=self.ollama_num_ctx,
                llm_max_prompt_chars=self.llm_max_prompt_chars,
                lease_mode="process_local" if self.tasks_always_eager else "redis",
                ollama_no_cloud=self.ollama_no_cloud,
            ):
                errors.append("qualified local-model report must be present, current, and passing")
        except (OSError, ValueError):
            errors.append(
                "OLLAMA_EXPECTED_MODEL_DIGEST must match the committed qualification registry"
            )
        unsafe_cors_origins = [
            origin for origin in self.cors_origin_list if not is_safe_production_cors_origin(origin)
        ]
        if unsafe_cors_origins:
            errors.append("CORS_ORIGINS must contain exact HTTPS or loopback HTTP origins")
        live_requested = not self.draft_only or not self.dry_run or self.portal_final_submit_enabled
        if live_requested and not self.live_automation_acknowledged:
            errors.append(
                "LIVE_AUTOMATION_ACKNOWLEDGED=true is required for non-dry-run automation"
            )
        if self.portal_final_submit_enabled and self.dry_run:
            errors.append("PORTAL_FINAL_SUBMIT_ENABLED cannot be used with DRY_RUN=true")
        if self.portal_final_submit_enabled and self.draft_only:
            errors.append("PORTAL_FINAL_SUBMIT_ENABLED cannot be used with DRAFT_ONLY=true")
        if self.portal_final_submit_enabled and not self.db_is_postgres:
            errors.append("PORTAL_FINAL_SUBMIT_ENABLED requires PostgreSQL")
        if self.submission_command_drain_interval_seconds >= self.submit_permit_ttl_seconds:
            errors.append("submission command drain interval must be below permit TTL")
        if not 1 <= self.submission_command_drain_batch_size <= 100:
            errors.append("submission command drain batch size must be between 1 and 100")
        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))

    @property
    def db_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def db_is_postgres(self) -> bool:
        return self.database_url.lower().startswith(("postgresql://", "postgresql+"))

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


def load_authoritative_settings(env_path: Path) -> Settings:
    """Load one explicit env file without consulting inherited process values.

    ``BaseSettings(_env_file=...)`` still gives operating-system environment
    variables precedence over the file. That is useful for ordinary
    deployments, but unsafe for the private runner: a stale parent shell must
    not turn off dry-run mode or redirect its database after the external
    runtime file has been reviewed. Direct model validation bypasses every
    settings source while retaining Pydantic's validation and type coercion.
    """

    if not env_path.is_absolute():
        raise ValueError(f"{JOB_AGENT_ENV_FILE} must be an absolute path")
    try:
        if not env_path.is_file() or not 0 < env_path.stat().st_size <= 64 * 1024:
            raise OSError
        raw_values = dotenv_values(
            dotenv_path=env_path,
            encoding="utf-8",
            interpolate=False,
        )
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{JOB_AGENT_ENV_FILE} is unavailable or invalid") from exc

    field_names = {name.casefold(): name for name in Settings.model_fields}
    values: dict[str, str] = {}
    for raw_name, raw_value in raw_values.items():
        field_name = field_names.get(raw_name.casefold())
        if field_name is not None and raw_value is not None:
            values[field_name] = raw_value
    return Settings.model_validate(values)


@lru_cache
def get_settings() -> Settings:
    """Singleton accessor for application settings."""

    configured = os.environ.get(JOB_AGENT_ENV_FILE, "").strip()
    if not configured:
        return Settings()
    env_path = Path(configured)
    if not env_path.is_absolute():
        raise ValueError(f"{JOB_AGENT_ENV_FILE} must be an absolute path")
    return load_authoritative_settings(env_path)

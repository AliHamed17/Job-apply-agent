"""Tests for runtime configuration safety checks."""

from core.config import Settings


class TestRuntimeConfigValidation:
    def test_prod_requires_secret_key(self):
        settings = Settings(app_env="prod", secret_key="change-me")
        errors = settings.validate_runtime_config()
        assert any("SECRET_KEY" in error for error in errors)

    def test_prod_requires_app_secret_when_whatsapp_token_is_set(self):
        settings = Settings(
            app_env="prod",
            secret_key="super-secret",
            whatsapp_api_token="token",
            whatsapp_app_secret="",
        )
        errors = settings.validate_runtime_config()
        assert any("WHATSAPP_APP_SECRET" in error for error in errors)

    def test_prod_valid_config_has_no_errors(self):
        settings = Settings(
            app_env="prod",
            secret_key="super-secret",
            whatsapp_api_token="token",
            whatsapp_app_secret="app-secret",
        )
        assert settings.validate_runtime_config() == []

    def test_dev_allows_default_secret_key(self):
        settings = Settings(app_env="dev", secret_key="change-me")
        assert settings.validate_runtime_config() == []

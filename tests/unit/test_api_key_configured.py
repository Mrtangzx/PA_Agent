"""Tests for API key presence helper."""

from __future__ import annotations

from pa_agent.config.settings import Settings, provider_api_key_configured


def test_codex_local_login_does_not_require_api_key() -> None:
    s = Settings()
    s.provider.api_key = ""
    assert provider_api_key_configured(s)
    assert not provider_api_key_configured(None)


def test_codex_ignores_legacy_whitespace_api_key() -> None:
    s = Settings()
    s.provider.api_key = "   "
    assert provider_api_key_configured(s)


def test_provider_api_key_configured_present() -> None:
    s = Settings()
    s.provider.api_key = "sk-test"
    assert provider_api_key_configured(s)


def test_codex_sdk_uses_local_login_without_api_key() -> None:
    s = Settings()
    s.provider.backend = "codex_sdk"
    s.provider.api_key = ""
    assert provider_api_key_configured(s)

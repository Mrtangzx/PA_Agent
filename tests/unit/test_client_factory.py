"""Tests for AI client factory routing."""

from __future__ import annotations

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.codex_sdk_client import CodexSdkClient
from pa_agent.config.settings import AIProviderSettings


def test_legacy_cursor_settings_are_forced_to_codex_sdk() -> None:
    settings = AIProviderSettings(
        model="openclaw_cs",
        base_url="",
        api_key="crsr_test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CodexSdkClient)
    assert settings.backend == "codex_sdk"
    assert settings.model == "gpt-5.6-terra"
    assert settings.base_url == ""
    assert settings.api_key == ""


def test_legacy_openai_compatible_settings_are_forced_to_codex_sdk() -> None:
    settings = AIProviderSettings(
        model="openclaw",
        base_url="http://127.0.0.1:19000/v1",
        api_key="test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CodexSdkClient)
    assert settings.backend == "codex_sdk"
    assert settings.model == "gpt-5.6-terra"
    assert settings.base_url == ""
    assert settings.api_key == ""


def test_create_ai_client_codex_backend_uses_codex_sdk() -> None:
    settings = AIProviderSettings(
        backend="codex_sdk",
        model="gpt-5.6-terra",
        base_url="",
        api_key="",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CodexSdkClient)


def test_explicit_codex_backend_wins_over_legacy_cursor_alias() -> None:
    settings = AIProviderSettings(
        backend="codex_sdk",
        model="openclaw_cs",
        api_key="",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CodexSdkClient)
    assert settings.model == "gpt-5.6-terra"

"""Architecture guardrails for the production Codex-only model route."""
from __future__ import annotations

import ast
from pathlib import Path

from pa_agent.config.settings import Settings, load_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_provider_json_is_migrated_and_sanitized(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"provider":{"backend":"openai_compatible","model":"deepseek-v4-flash",'
        '"base_url":"https://api.deepseek.com","api_key":"secret"}}',
        encoding="utf-8",
    )

    loaded = load_settings(path)

    assert loaded.provider.backend == "codex_sdk"
    assert loaded.provider.model == "gpt-5.6-terra"
    assert loaded.provider.base_url == ""
    assert loaded.provider.api_key == ""
    assert loaded.provider.reasoning_effort == "high"
    persisted = path.read_text(encoding="utf-8")
    assert '"api_key": "secret"' not in persisted


def test_production_entrypoints_do_not_import_legacy_provider_routes() -> None:
    banned_modules = {
        "pa_agent.ai.deepseek_client",
        "pa_agent.ai.cursor_connector",
        "pa_agent.ai.cursor_sdk_client",
        "pa_agent.ai.qclaw_connector",
        "pa_agent.ai.workbuddy_connector",
    }
    files = [
        "pa_agent/app_context.py",
        "pa_agent/ai/client_factory.py",
        "pa_agent/orchestrator/two_stage.py",
        "pa_agent/orchestrator/free_chat.py",
        "pa_agent/gui/ai_model_settings_dialog.py",
        "pa_agent/gui/settings_dialog.py",
        "pa_agent/ai/codex_sdk_client.py",
        "pa_agent/ai/session_ledger.py",
        "pa_agent/ai/prompt_assembler.py",
        "pa_agent/gui/main_window.py",
    ]
    violations: list[str] = []
    for relative in files:
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in banned_modules:
                violations.append(f"{relative}:{node.lineno}:{node.module}")
    assert violations == []


def test_codex_defaults_are_fixed() -> None:
    provider = Settings().provider
    assert provider.backend == "codex_sdk"
    assert provider.model == "gpt-5.6-terra"
    assert provider.base_url == ""
    assert provider.api_key == ""
    assert provider.reasoning_effort == "high"
